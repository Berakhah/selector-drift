"""Day one: the cron fires, the target is authorised, the start page loads, a run record is
written. No cascade exists yet. Task 26A replaces everything after the authorisation block with
pipeline.run_once(); the heartbeat prologue stays first.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from prefect import flow
from prefect.runtime import flow_run

from selector_drift.browser import driver  # attribute access, so tests can patch driver.open
from selector_drift.browser.limits import FLOW_TIMEOUT_SECONDS  # B8: 600 s (Task 4)
from selector_drift.orchestrate.run_record import (
    heartbeat,
    link_heartbeat,
    next_attempt,
    record_attempt,
    record_authorisation,
    record_run,
    run_key,
    window_for,
)
from selector_drift.quality.alerts import post_alert, sanitize_for_alert
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import AuthorisedTarget
from selector_drift.spec.loader import load_scope, load_spec, scope_file, spec_file, target_of

if TYPE_CHECKING:
    from prefect.client.schemas.objects import FlowRun, State
    from prefect.flows import Flow

RETRIES = 2
RETRY_DELAY_SECONDS = 300

_log = logging.getLogger(__name__)

# Plus item (final review), keyed on the attempt identity (hookfix brief): set to the current
# attempt's `run_count` just before the flow body's own `except Exception` branch posts its alert
# and re-raises, so `alert_on_flow_failure` (the on_failure hook, below) can tell "the flow body
# already alerted for *this exact attempt*" from "the flow failed before reaching that branch at
# all" (a SpecError loading the spec, a DB outage during the heartbeat or authorisation
# bookkeeping, an invalid run key) and only alert in the latter case. A ContextVar, not a plain
# module global: Prefect gives each flow run its own copy of the current context, so concurrent
# flow runs (three targets, each on its own schedule) never see each other's flag.
#
# This must be the attempt's identity, not a bare bool: Prefect retries re-enter extraction_flow
# in the same process/context, so a bool set True on one attempt stays True on a later attempt
# that fails for a *different* reason before ever reaching this branch again -- silently
# suppressing the hook's alert for the failure that actually ended the flow run. Comparing
# run_count against the hook's own `flow_run.run_count` (the attempt that actually failed last)
# closes that gap: the hook skips only when the two identify the same attempt.
_ALERTED_IN_FLOW: ContextVar[int | None] = ContextVar("_ALERTED_IN_FLOW", default=None)


class StartPageError(RuntimeError):
    """The authorised start page did not return an HTML document."""


def scheduled_slot() -> datetime:
    """The cron slot this run belongs to, in UTC. Outside a flow run Prefect returns now()."""
    slot = flow_run.scheduled_start_time
    return datetime.fromtimestamp(slot.timestamp(), tz=UTC)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


async def load_start_page(target: AuthorisedTarget) -> int:
    # browser.ratelimit.acquire() (called inside driver.open()) blocks synchronously in a worker
    # thread while it waits out the target's rate limit; that wait counts against this flow's
    # 600 s timeout (ruling 5), same as any other time spent inside the `async with` below.
    async with driver.open(target) as session:
        html = await session.page.content()
    if "<html" not in html.lower():
        raise StartPageError("the start page did not return an HTML document")
    return len(html)


def alert_on_flow_failure(flow: Flow[Any, Any], flow_run: FlowRun, state: State[Any]) -> None:
    """Prefect `on_failure` hook (plus item, final review): catches every failure path the flow
    body's own `except Exception` block does not wrap -- a `SpecError` loading the spec, a DB
    outage before or during the heartbeat or authorisation bookkeeping, or an invalid run key.
    Deferred Task 6 finding: on these paths, the only alert channel stayed silent, leaving a gap
    in the 90-day log with nobody told.

    Skips alerting only when `_ALERTED_IN_FLOW` records the run_count of *this* attempt --
    `flow_run.run_count`, the attempt that actually failed last: the flow body's own
    post-authorisation `except Exception` branch already posted an alert for this exact attempt
    before re-raising, and posting again here would be a second alert for one event (M-8 already
    flags alert volume as a separate concern; this hook must not add to it for the common case).
    Any other value -- None, or a different attempt's run_count -- means this attempt's failure
    was never alerted in-body, so the hook must alert.

    Best-effort and defensive: this must never itself raise into Prefect's state machine, so
    any failure while building or sending the alert is only logged.
    """
    if _ALERTED_IN_FLOW.get() == flow_run.run_count:
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
    day = date.fromisoformat(window)

    try:
        record = load_scope(scope_file(spec.scope_ref))
        if record.target != stem:
            raise ScopeRefusal(
                f"spec {spec_path} is for target {stem!r} but its scope record "
                f"names {record.target!r}"
            )
        target = authorise(record, today=day)  # UTC date of the slot (M20)
    except ScopeRefusal as exc:
        record_run(
            key,
            target=stem,
            spec_version=spec.version,
            window=day,
            state="refused",
            error=exc.reason,
        )
        post_alert("refused", stem, key, exc.reason)
        return key  # no attempt row, no browser

    attempt = next_attempt(key)
    record_attempt(key, attempt, "started", None, target.record_sha256)
    record_authorisation(key, target.target, target.record_sha256, record=record)
    try:
        asyncio.run(load_start_page(target))
    except Exception as exc:
        error = _describe(exc)
        record_attempt(key, attempt, "failed", error, target.record_sha256)
        record_run(
            key, target=stem, spec_version=spec.version, window=day, state="failed", error=error
        )
        # I-2 (final review): the alert webhook is third-party and outside the client boundary
        # (CLAUDE.md rule 6) -- `error` (raw `str(exc)`) is what stays in run_attempts.error /
        # runs.error, but the alert body carries only the sanitised summary. A Playwright locator
        # timeout's "Call log:" section routinely quotes DOM content.
        post_alert("failed", stem, key, sanitize_for_alert(exc))
        # Records *this* attempt's run_count, verified empirically to agree with the hook's own
        # flow_run.run_count for the final attempt (see hookfix-report.md) -- tells
        # alert_on_flow_failure not to alert a second time for this same attempt.
        _ALERTED_IN_FLOW.set(flow_run.run_count)
        raise  # Prefect's retry policy applies
    record_attempt(key, attempt, "completed", None, target.record_sha256)
    record_run(key, target=stem, spec_version=spec.version, window=day, state="completed")
    return key
