"""Login, logout, me. argon2id; hashed session tokens; a throttle that survives restarts."""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import secrets
import threading
from collections import OrderedDict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as OrmSession

from selector_drift.api.deps import SESSION_COOKIE, CurrentOperator, Db, token_sha256
from selector_drift.api.errors import Invalid, Throttled, Unauthenticated
from selector_drift.api.views import LoginRequest, OperatorView
from selector_drift.db.models import AuditLog, Operator, OperatorSession

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_LIFETIME = timedelta(hours=12)  # absolute; no sliding renewal
THROTTLE_LIMIT = 5
THROTTLE_WINDOW = timedelta(minutes=15)
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
_hasher = PasswordHasher()  # argon2id, library defaults
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))  # equalises timing for unknown users
# Each argon2id verify allocates ~64 MiB. Cap concurrent verifies per process so rotating
# usernames cannot exhaust memory; a full cap is refused at once, never queued. Per PROCESS:
# N uvicorn workers allow N * VERIFY_CAP.
VERIFY_CAP = 4
_verify_slots = threading.BoundedSemaphore(VERIFY_CAP)

_log = logging.getLogger(__name__)
# Unknown usernames are attacker-controlled, so they are throttled in process memory only:
# keyed by an HMAC under a per-process random key (the name itself is never stored or logged),
# bounded, and behind one lock. Per PROCESS: N workers each keep their own table.
UNKNOWN_KEYS_CAP = 10_000
_unknown_lock = threading.Lock()
_unknown_key = secrets.token_bytes(32)
_unknown_hits: OrderedDict[bytes, deque[datetime]] = OrderedDict()
_unknown_total = 0


def unknown_user_failures() -> int:
    """Failed sign-ins for usernames with no operator row, since process start."""
    with _unknown_lock:
        return _unknown_total


def _unknown_digest(username: str) -> bytes:
    return hmac.new(_unknown_key, username.encode(), hashlib.sha256).digest()


def _unknown_wait(username: str, now: datetime) -> int | None:
    """Seconds until an unknown username may try again, or None if it is under the limit."""
    key = _unknown_digest(username)
    since = now - THROTTLE_WINDOW
    with _unknown_lock:
        hits = _unknown_hits.get(key)
        if hits is None:
            return None
        while hits and hits[0] <= since:
            hits.popleft()
        if not hits:
            del _unknown_hits[key]
            return None
        if len(hits) >= THROTTLE_LIMIT:
            return max(1, math.ceil((hits[0] + THROTTLE_WINDOW - now).total_seconds()))
        return None


def _unknown_record(username: str, now: datetime) -> int:
    """Record one failure for an unknown username; return the running total."""
    global _unknown_total
    key = _unknown_digest(username)
    with _unknown_lock:
        hits = _unknown_hits.get(key)
        if hits is None:
            while len(_unknown_hits) >= UNKNOWN_KEYS_CAP:
                _unknown_hits.popitem(last=False)  # evict the oldest key
            hits = _unknown_hits[key] = deque()
        hits.append(now)
        _unknown_total += 1
        return _unknown_total


def audit(
    db: OrmSession,
    *,
    actor: str,
    action: str,
    subject: str,
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            at=datetime.now(UTC), actor=actor, action=action, subject=subject, detail=detail or {}
        )
    )


def create_operator(db: OrmSession, username: str, password: str) -> Operator:
    if not USERNAME_RE.fullmatch(username):
        raise Invalid("username must match ^[a-z0-9_.-]{1,64}$")
    op = Operator(
        username=username,
        password_hash=_hasher.hash(password),
        created_at=datetime.now(UTC),
        disabled_at=None,
    )
    db.add(op)
    audit(
        db,
        actor="system:create-operator",
        action="operator_created",
        subject=f"operator:{username}",
    )
    db.flush()
    return op


def unlock_operator(db: OrmSession, username: str) -> None:
    """Reset the login throttle for one operator. History is kept; the reset is audited."""
    if db.scalars(select(Operator).where(Operator.username == username)).first() is None:
        raise Invalid(f"no such operator: {username}")
    audit(
        db,
        actor="system:unlock-operator",
        action="login_unlock",
        subject=f"operator:{username}",
    )


def _failures(db: OrmSession, username: str, now: datetime) -> tuple[int, datetime | None]:
    subject = f"operator:{username}"
    since = now - THROTTLE_WINDOW
    last_unlock = db.scalar(
        select(func.max(AuditLog.at)).where(
            AuditLog.action == "login_unlock", AuditLog.subject == subject
        )
    )
    # A future-dated unlock (clock skew between the CLI and API hosts) is ignored: honouring it
    # would hide every failure up to that instant, including the ones still to come.
    if last_unlock is not None and since < last_unlock <= now:
        since = last_unlock
    q = select(func.count(), func.min(AuditLog.at)).where(
        AuditLog.action == "login_failure",
        AuditLog.subject == subject,
        AuditLog.at > since,
    )
    count, oldest = db.execute(q).one()
    return int(count), oldest


def authenticate(db: OrmSession, username: str, password: str, now: datetime) -> Operator:
    # Serialise attempts per username: lock -> count -> verify -> failure row is one critical
    # section, else N concurrent requests all see count < limit and each gets a guess. It is a
    # TRY lock: a loser is refused at once instead of parking a pooled connection and a thread
    # behind a slow argon2 verify. The xact lock is released by the failure-row commit, by
    # issue_session's commit on success, and by the explicit rollbacks on the refusal paths.
    got = db.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": f"login:{username}"}
    ).scalar_one()
    if not got:
        db.rollback()
        raise Throttled(
            "Another sign-in attempt for this username is in progress - try again shortly",
            headers={"Retry-After": "1"},
        )
    count, oldest = _failures(db, username, now)
    if count >= THROTTLE_LIMIT and oldest is not None:
        db.rollback()
        wait = max(1, math.ceil((oldest + THROTTLE_WINDOW - now).total_seconds()))
        raise Throttled(
            f"Too many attempts - try again in {math.ceil(wait / 60)} minutes",
            headers={"Retry-After": str(wait)},
        )
    op = db.scalars(select(Operator).where(Operator.username == username)).first()
    if op is None:
        unknown_wait = _unknown_wait(username, now)
        if unknown_wait is not None:  # identical to the real-user refusal above
            db.rollback()
            raise Throttled(
                f"Too many attempts - try again in {math.ceil(unknown_wait / 60)} minutes",
                headers={"Retry-After": str(unknown_wait)},
            )
    usable = op is not None and op.disabled_at is None
    if not _verify_slots.acquire(blocking=False):
        db.rollback()
        raise Throttled(
            "The server is busy verifying other sign-ins - try again shortly",
            headers={"Retry-After": "1"},
        )
    ok: bool
    try:
        ok = _hasher.verify(op.password_hash if usable and op else _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        ok = False
    finally:
        _verify_slots.release()
    if not (ok and usable and op):
        if op is None:
            # Unknown names are attacker-controlled: count them, never persist or log them.
            total = _unknown_record(username, now)
            _log.warning("login failure for an unknown username (count=%d)", total)
            db.rollback()  # releases the xact lock; nothing to persist
        else:
            audit(db, actor="anonymous", action="login_failure", subject=f"operator:{username}")
            db.commit()
        raise Unauthenticated("Username or password is incorrect")
    if _hasher.check_needs_rehash(op.password_hash):
        op.password_hash = _hasher.hash(password)
    return op


def issue_session(db: OrmSession, operator: Operator, now: datetime) -> str:
    raw = secrets.token_urlsafe(32)  # 32 random bytes; only its SHA-256 is stored
    db.add(
        OperatorSession(
            token_sha256=token_sha256(raw),
            operator_id=operator.id,
            created_at=now,
            expires_at=now + SESSION_LIFETIME,
            revoked_at=None,
        )
    )
    audit(
        db,
        actor=operator.username,
        action="login_success",
        subject=f"operator:{operator.username}",
    )
    db.commit()
    return raw


@router.post("/login", response_model=OperatorView)
def login(body: LoginRequest, response: Response, db: Db) -> OperatorView:
    now = datetime.now(UTC)
    op = authenticate(db, body.username, body.password, now)
    raw = issue_session(db, op, now)
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return OperatorView(username=op.username)


@router.post("/logout", status_code=204, response_class=Response)
def logout(request: Request, db: Db, op: CurrentOperator) -> Response:
    raw = request.cookies.get(SESSION_COOKIE) or ""
    row = db.get(OperatorSession, token_sha256(raw))
    if row is not None:
        row.revoked_at = datetime.now(UTC)
    audit(db, actor=op.username, action="logout", subject=f"operator:{op.username}")
    db.commit()
    out = Response(status_code=204)
    out.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return out


@router.get("/me", response_model=OperatorView)
def me(op: CurrentOperator) -> OperatorView:
    return OperatorView(username=op.username)
