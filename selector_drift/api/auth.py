"""Login, logout, me. argon2id; hashed session tokens; a throttle that survives restarts."""

from __future__ import annotations

import math
import re
import secrets
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
_USERNAME = re.compile(r"^[a-z0-9_.-]{1,64}$")
_hasher = PasswordHasher()  # argon2id, library defaults
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))  # equalises timing for unknown users


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
    if not _USERNAME.fullmatch(username):
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


def _failures(db: OrmSession, username: str, now: datetime) -> tuple[int, datetime | None]:
    since = now - THROTTLE_WINDOW
    q = select(func.count(), func.min(AuditLog.at)).where(
        AuditLog.action == "login_failure",
        AuditLog.subject == f"operator:{username}",
        AuditLog.at >= since,
    )
    count, oldest = db.execute(q).one()
    return int(count), oldest


def authenticate(db: OrmSession, username: str, password: str, now: datetime) -> Operator:
    # Serialise attempts per username: count -> verify -> failure row is one critical section,
    # else N concurrent requests all see count < limit and each gets a guess. The xact lock is
    # released by the commit on failure / by issue_session's commit on success; the throttled
    # path rolls back explicitly.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"login:{username}"})
    count, oldest = _failures(db, username, now)
    if count >= THROTTLE_LIMIT and oldest is not None:
        db.rollback()
        wait = max(1, math.ceil((oldest + THROTTLE_WINDOW - now).total_seconds()))
        raise Throttled(
            f"Too many attempts - try again in {math.ceil(wait / 60)} minutes",
            headers={"Retry-After": str(wait)},
        )
    op = db.scalars(select(Operator).where(Operator.username == username)).first()
    usable = op is not None and op.disabled_at is None
    ok: bool
    try:
        ok = _hasher.verify(op.password_hash if usable and op else _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        ok = False
    if not (ok and usable and op):
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
