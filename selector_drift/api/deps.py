"""Path validators, the db session, and the authenticated operator (API spec 3, 4.1)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from selector_drift.api.errors import Invalid, Unauthenticated
from selector_drift.config import Settings
from selector_drift.db.models import Operator, OperatorSession
from selector_drift.db.session import Session

TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RUN_KEY_RE = re.compile(r"^[a-z0-9-]{1,63}:\d{1,6}:\d{4}-\d{2}-\d{2}$")  # Task 6 run_key()
SESSION_COOKIE = "sd_session"


def check_target(value: str) -> str:
    if not TARGET_RE.fullmatch(value):
        raise Invalid("target: not a target name")
    return value


def check_run_key(value: str) -> str:
    if not RUN_KEY_RE.fullmatch(value):
        raise Invalid("run_key: not a run key")
    return value


def valid_target(target: str) -> str:  # bound by name to the {target} path param
    return check_target(target)


def valid_run_key(run_key: str) -> str:  # bound by name to the {run_key} path param
    return check_run_key(run_key)


def get_db() -> Iterator[OrmSession]:
    with Session() as s:
        yield s


def app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def token_sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_session(db: OrmSession, raw: str | None, now: datetime) -> Operator | None:
    if not raw or len(raw) > 128:
        return None
    row = db.get(OperatorSession, token_sha256(raw))
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    op = db.scalars(select(Operator).where(Operator.id == row.operator_id)).first()
    if op is None or op.disabled_at is not None:
        return None
    return op


Db = Annotated[OrmSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(app_settings)]
RunKey = Annotated[str, Depends(valid_run_key)]
TargetName = Annotated[str, Depends(valid_target)]


def current_operator(request: Request, db: Db) -> Operator:
    op = resolve_session(db, request.cookies.get(SESSION_COOKIE), datetime.now(UTC))
    if op is None:
        raise Unauthenticated("sign in to continue")
    return op


CurrentOperator = Annotated[Operator, Depends(current_operator)]
