"""Run bookkeeping: the run key, one row per attempt, one heartbeat per fire, the terminal state.

Separate from flows.py so that pipeline.py (Task 26A), which flows.py imports, can use it too.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import func, select

from selector_drift.db.models import AuditLog, Heartbeat, Run, RunAttempt, ScopeRecordRow
from selector_drift.db.session import Session
from selector_drift.scope.record import TARGET_SLUG, ScopeRecord

TerminalState = Literal["completed", "healed", "quarantined", "failed", "refused"]
AttemptState = Literal["started", "completed", "healed", "quarantined", "failed"]

NO_TERMINAL_STATE = "attempt started; no terminal state was recorded (crash, kill or timeout)"
MAX_ERROR = 2000
# M-3 (final review): shared with scope.record.ScopeRecord's target validator, so a target that
# passes the scope gate can never fail here with an uncaught ValueError after the heartbeat has
# already been written.
_TARGET = TARGET_SLUG


def run_key(target: str, spec_version: int, window: str) -> str:
    if not _TARGET.fullmatch(target):
        raise ValueError(f"target {target!r} is not a run-key target (lower-case, digits, '-')")
    if spec_version < 1:
        raise ValueError(f"spec_version {spec_version} is below 1")
    try:
        exact = date.fromisoformat(window).isoformat() == window
    except ValueError:
        exact = False
    if not exact:
        raise ValueError(f"window {window!r} is not a YYYY-MM-DD date")
    return f"{target}:{spec_version}:{window}"


def parse_run_key(key: str) -> tuple[str, int, date]:
    parts = key.split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        raise ValueError(f"{key!r} is not target:spec_version:YYYY-MM-DD")
    target, version, window = parts[0], int(parts[1]), parts[2]
    run_key(target, version, window)  # same rules both ways
    return target, version, date.fromisoformat(window)


def window_for(slot: datetime) -> str:
    """The UTC date of the cron slot, not of the fire (M20)."""
    if slot.tzinfo is None:
        raise ValueError("a naive datetime cannot be placed in a UTC window (M20)")
    return slot.astimezone(UTC).date().isoformat()


def _clip(error: str | None) -> str | None:
    return None if error is None else error[:MAX_ERROR]


def heartbeat(
    target: str, scheduled_for: datetime, run_key: str | None, *, fired_at: datetime | None = None
) -> int:
    """One row per (target, cron slot). A retry of the same slot returns the existing row."""
    if scheduled_for.tzinfo is None:
        raise ValueError("scheduled_for must be timezone-aware (M20)")
    with Session() as s:
        existing = s.scalar(
            select(Heartbeat).where(
                Heartbeat.target == target, Heartbeat.scheduled_for == scheduled_for
            )
        )
        if existing is not None:
            if run_key is not None and existing.run_key is None:
                existing.run_key = run_key
                s.commit()
            return existing.id
        row = Heartbeat(
            target=target,
            scheduled_for=scheduled_for,
            fired_at=fired_at or datetime.now(UTC),
            run_key=run_key,
        )
        s.add(row)
        s.commit()
        return row.id


def link_heartbeat(heartbeat_id: int, run_key: str) -> None:
    with Session() as s:
        row = s.get(Heartbeat, heartbeat_id)
        if row is None:
            raise LookupError(f"no heartbeat {heartbeat_id}")
        row.run_key = run_key
        s.commit()


def next_attempt(run_key: str) -> int:
    with Session() as s:
        latest = s.scalar(select(func.max(RunAttempt.attempt)).where(RunAttempt.run_key == run_key))
    return (latest or 0) + 1


def record_attempt(
    run_key: str, attempt: int, state: AttemptState, error: str | None, scope_record_sha256: str
) -> None:
    """Call only after authorise() returns: a refusal has no attempt (Task 34B)."""
    now = datetime.now(UTC)
    with Session() as s:
        run = s.get(Run, run_key)
        if run is None:
            target, version, window = parse_run_key(run_key)
            run = Run(
                run_key=run_key,
                target=target,
                spec_version=version,
                window=window,
                first_started_at=now,
                last_finished_at=None,
                state="failed",
                rows_written=None,
                error=NO_TERMINAL_STATE,
            )
            s.add(run)
            s.flush()
        if state == "started":
            run.state, run.error = "failed", NO_TERMINAL_STATE  # until a terminal state lands
        row = s.scalar(
            select(RunAttempt).where(RunAttempt.run_key == run_key, RunAttempt.attempt == attempt)
        )
        if row is None:
            row = RunAttempt(
                run_key=run_key,
                attempt=attempt,
                started_at=now,
                finished_at=None,
                state=state,
                error=None,
                scope_record_sha256=scope_record_sha256,
            )
            s.add(row)
        row.state = state
        row.error = _clip(error)
        row.finished_at = None if state == "started" else now
        s.commit()


def record_run(
    key: str,
    *,
    target: str,
    spec_version: int,
    window: date,
    state: TerminalState,
    rows_written: int | None = None,
    error: str | None = None,
) -> None:
    if run_key(target, spec_version, window.isoformat()) != key:
        raise ValueError(f"run key {key!r} does not match {target}/{spec_version}/{window}")
    now = datetime.now(UTC)
    with Session() as s:
        run = s.get(Run, key)
        if run is None:
            run = Run(
                run_key=key,
                target=target,
                spec_version=spec_version,
                window=window,
                first_started_at=now,
            )
            s.add(run)
        run.state = state
        run.last_finished_at = now
        run.rows_written = rows_written
        run.error = _clip(error)
        s.commit()


def record_authorisation(
    run_key: str, target: str, record_sha256: str, *, record: ScopeRecord
) -> None:
    """The audit row, and the scope_records mirror the read side shows (found 2026-09-26: the
    mirror had no writer). Both in one transaction: a mirror newer than the audit is a lie.

    The audit row's detail carries the full validated `ScopeRecord`, not just its target and
    hash (final-review ruling 2026-09-28): "which authorisation was in force" must be answerable
    without joining out to `scope_records`, whose row a later retention job may have pruned."""
    now = datetime.now(UTC)
    with Session() as s:
        s.add(
            AuditLog(
                at=now,
                actor="scheduler",
                action="scope_authorised",
                subject=run_key,
                detail={
                    "target": target,
                    "record_sha256": record_sha256,
                    "record": record.model_dump(mode="json"),
                },
            )
        )
        s.merge(
            ScopeRecordRow(
                target=record.target,
                basis=record.basis.value,
                authorising_party=record.authorising_party,
                authorised_on=record.authorised_on,
                expires_on=record.expires_on,
                evidence_uri=record.evidence_uri,
                evidence_sha256=record.evidence_sha256,
                origins=list(record.origins),
                rate_limit={
                    "requests": record.rate_limit.requests,
                    "per_seconds": record.rate_limit.per_seconds,
                },
                user_agent_contact=record.user_agent_contact,
                first_heal_policy=record.first_heal_policy.value,
                legal_review=record.legal_review,
                warmup_runs=record.warmup_runs,
                evidence_retention_days=record.evidence_retention_days,
                credential_posture=record.credential_posture,
                record_sha256=record_sha256,
                loaded_at=now,
            )
        )
        s.commit()
