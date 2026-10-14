"""The scheduled run. Task 6's prologue is unchanged -- heartbeat first, then the spec, the run
key and link_heartbeat -- and pipeline.run_once() now does everything after it (Task 26A): the
scope gate, the browser, the cascade, the gate, the lake, quality and evidence.

This module owns the Prefect concerns: retries, the 600 s timeout, and recording a failure --
in the body for an exception run_once raised, in the on_failure hook for one it never saw.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from prefect import flow
from prefect.runtime import flow_run
from sqlalchemy import func, select

from selector_drift.browser.limits import FLOW_TIMEOUT_SECONDS  # B8: 600 s (Task 4)
from selector_drift.credentials import registry  # module attribute: tests patch provider_for
from selector_drift.db.models import RunAttempt
from selector_drift.db.session import Session
from selector_drift.orchestrate.pipeline import default_confirmers, run_for, run_once
from selector_drift.orchestrate.run_record import (
    heartbeat,
    link_heartbeat,
    parse_run_key,
    record_attempt,
    record_run,
    run_key,
    window_for,
)
from selector_drift.quality.alerts import notify, post_alert, sanitize_for_alert
from selector_drift.spec.loader import load_spec, spec_file, target_of

if TYPE_CHECKING:
    from prefect.client.schemas.objects import FlowRun, State
    from prefect.flows import Flow

RETRIES = 2
RETRY_DELAY_SECONDS = 300

_log = logging.getLogger(__name__)

# Plus item (final review), keyed on run identity plus attempt (hookfix brief, round 1): set to
# the current attempt's `(flow_run.id, run_count)` just before the flow body's own
# `except Exception` branch posts its alert and re-raises, so `alert_on_flow_failure` (the
# on_failure hook, below) can tell "the flow body already alerted for *this exact attempt of this
# exact flow run*" from "the flow failed before reaching that branch at all" (a SpecError
# loading the spec, a DB outage during the heartbeat or authorisation bookkeeping, an invalid run
# key) and only alert in the latter case.
#
# It must be keyed on run identity, not just the attempt number: Prefect 3.8.4 runs a *sync* flow
# body in the caller's context, with no `copy_context()` (flow_engine.py: call_with_parameters) --
# so this ContextVar is shared across sequential in-process flow runs in the same worker/process,
# not scoped to one flow run the way a fresh `contextvars.Context` would be. A round-1 review
# caught this: run_count alone repeats across separate flow runs (every run's first attempt is
# run_count 1, its second is 2, ...), so a value set by one flow run's in-body alert can equal a
# *later, unrelated* flow run's `flow_run.run_count` and wrongly suppress that later run's hook
# alert, even though that later run's final attempt never reached the in-body alert branch at
# all. Including `flow_run.id` closes that gap: two different flow runs never share an id, so the
# hook skips only when both the run and the attempt match.
#
# `prefect.runtime.flow_run.id` is a `str`; the hook's `FlowRun.id` is a `uuid.UUID` -- equal in
# value but not by `==` unless cast, so the body wraps it in `UUID(...)` before storing (verified
# empirically: `str(uuid) == uuid` is False, since `UUID.__eq__` requires `isinstance(other,
# UUID)`, so an uncast `str` would silently defeat this whole comparison).
_ALERTED_IN_FLOW: ContextVar[tuple[UUID, int] | None] = ContextVar("_ALERTED_IN_FLOW", default=None)


def scheduled_slot() -> datetime:
    """The cron slot this run belongs to, in UTC. Outside a flow run Prefect returns now()."""
    slot = flow_run.scheduled_start_time
    return datetime.fromtimestamp(slot.timestamp(), tz=UTC)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _open_attempt(key: str) -> RunAttempt | None:
    """The latest attempt, if it started and nothing settled it. A refusal has no attempt, and
    a settled attempt must never be re-marked failed."""
    with Session() as s:
        latest = s.scalar(select(func.max(RunAttempt.attempt)).where(RunAttempt.run_key == key))
        if latest is None:
            return None
        row = s.scalar(
            select(RunAttempt).where(RunAttempt.run_key == key, RunAttempt.attempt == latest)
        )
        if row is None or row.state != "started":
            return None
        s.expunge(row)
        return row


def _record_failure(attempt: RunAttempt, error: str) -> None:
    """Design 9.3 Fail: the attempt and the run are marked failed (the full text stays inside
    the boundary, in run_attempts.error / runs.error), and the run is notified (Task 29A)."""
    target, version, window = parse_run_key(attempt.run_key)
    record_attempt(attempt.run_key, attempt.attempt, "failed", error, attempt.scope_record_sha256)
    record_run(
        attempt.run_key, target=target, spec_version=version, window=window, state="failed",
        error=error,
    )  # fmt: skip
    notify([], run_for(attempt.run_key))


def _record_unseen_failure(flow_run: FlowRun, state: State[Any]) -> None:
    """A crash or timeout the body's `except` never saw leaves the attempt `started`; record it
    failed now (Task 26A). Best-effort: the hook must never raise."""
    try:
        spec_path = str(flow_run.parameters["spec_path"])
        slot = (flow_run.expected_start_time or datetime.now(UTC)).astimezone(UTC)
        key = run_key(target_of(spec_path), load_spec(spec_file(spec_path)).version,
                      window_for(slot))  # fmt: skip
        attempt = _open_attempt(key)
        if attempt is not None:
            _record_failure(
                attempt, state.message or "the flow run failed without a recorded error"
            )
    except Exception:
        _log.error("on_failure hook could not record the open attempt")


def alert_on_flow_failure(flow: Flow[Any, Any], flow_run: FlowRun, state: State[Any]) -> None:
    """Prefect `on_failure` hook (plus item, final review): catches every failure path the flow
    body's own `except Exception` block does not wrap -- a `SpecError` loading the spec, a DB
    outage before or during the heartbeat or authorisation bookkeeping, or an invalid run key.
    Deferred Task 6 finding: on these paths, the only alert channel stayed silent, leaving a gap
    in the 90-day log with nobody told.

    Skips alerting only when `_ALERTED_IN_FLOW` records `(flow_run.id, flow_run.run_count)` for
    *this exact attempt of this exact flow run* -- the run and attempt that actually failed last:
    the flow body's own post-authorisation `except Exception` branch already posted an alert for
    this exact attempt before re-raising, and posting again here would be a second alert for one
    event (M-8 already flags alert volume as a separate concern; this hook must not add to it for
    the common case). Any other value -- None, a different attempt of this run, or an attempt of
    a *different* flow run entirely -- means this attempt's failure was never alerted in-body, so
    the hook must alert. The run identity matters because the flag is a ContextVar shared across
    sequential in-process flow runs (see the comment above `_ALERTED_IN_FLOW`); run_count alone
    repeats across runs and is not enough to tell them apart.

    Best-effort and defensive: this must never itself raise into Prefect's state machine, so
    any failure while building or sending the alert is only logged.
    """
    _record_unseen_failure(flow_run, state)
    if _ALERTED_IN_FLOW.get() == (flow_run.id, flow_run.run_count):
        return
    spec_path = flow_run.parameters.get("spec_path")
    stem = target_of(spec_path) if isinstance(spec_path, str) and spec_path else "unknown"
    try:
        exc = state.result(raise_on_failure=False)
    except Exception:  # pragma: no cover -- state.result() itself should not raise here
        exc = None
    summary = sanitize_for_alert(exc) if isinstance(exc, BaseException) else "flow failed"
    try:
        # run_key is not known here (the whole point of this hook is failures where the flow
        # body's own bookkeeping never got that far) -- None, per the plus item's instructions.
        post_alert("failed", stem, None, summary)
    except Exception:
        _log.error("on_failure alert hook itself failed for target %s", stem)


@flow(
    name="extraction",
    retries=RETRIES,
    retry_delay_seconds=RETRY_DELAY_SECONDS,
    timeout_seconds=FLOW_TIMEOUT_SECONDS,
    on_failure=[alert_on_flow_failure],
)
def extraction_flow(spec_path: str) -> str:
    slot = scheduled_slot()
    stem = target_of(spec_path)
    beat = heartbeat(stem, slot, None)  # FIRST statement (API spec 6.1, B3)
    spec = load_spec(spec_file(spec_path))  # SpecError: heartbeat stays, run_key NULL
    window = window_for(slot)
    key = run_key(stem, spec.version, window)
    link_heartbeat(beat, key)
    try:
        asyncio.run(
            run_once(
                spec_path,
                window=date.fromisoformat(window),
                credentials_for=registry.provider_for,
                confirmers=default_confirmers(),
            )
        )
    except Exception as exc:
        attempt = _open_attempt(key)
        if attempt is None:
            raise  # failed before authorise() opened an attempt: the on_failure hook alerts
        _record_failure(attempt, _describe(exc))
        # I-2 (final review): the alert webhook is third-party and outside the client boundary
        # (CLAUDE.md rule 6) -- the raw `str(exc)` stays in run_attempts.error / runs.error,
        # the alert carries only the sanitised summary. A Playwright locator timeout's
        # "Call log:" section routinely quotes DOM content.
        post_alert("failed", stem, key, sanitize_for_alert(exc))
        # Tells alert_on_flow_failure not to alert again for this same attempt of this same
        # flow run. `flow_run.id` here is a `str`; the hook's `FlowRun.id` is a `uuid.UUID` --
        # cast so they compare equal (verified empirically, not assumed).
        _ALERTED_IN_FLOW.set((UUID(flow_run.id), flow_run.run_count))
        raise  # Prefect's retry policy applies
    return key
