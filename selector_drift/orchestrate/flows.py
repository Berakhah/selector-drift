"""Day one: the cron fires, the target is authorised, the start page loads, a run record is
written. No cascade exists yet. Task 26A replaces everything after the authorisation block with
pipeline.run_once(); the heartbeat prologue stays first.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

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
from selector_drift.quality.alerts import post_alert
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import AuthorisedTarget
from selector_drift.spec.loader import load_scope, load_spec, scope_file, spec_file, target_of

RETRIES = 2
RETRY_DELAY_SECONDS = 300


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


@flow(
    name="extraction",
    retries=RETRIES,
    retry_delay_seconds=RETRY_DELAY_SECONDS,
    timeout_seconds=FLOW_TIMEOUT_SECONDS,
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
        post_alert("failed", stem, key, error)
        raise  # Prefect's retry policy applies
    record_attempt(key, attempt, "completed", None, target.record_sha256)
    record_run(key, target=stem, spec_version=spec.version, window=day, state="completed")
    return key
