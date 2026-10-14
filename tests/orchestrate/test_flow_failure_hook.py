"""Plus item (final review): `alert_on_flow_failure` is the Prefect `on_failure` hook that
covers the failure paths `extraction_flow`'s own `except Exception` block never reaches --
a SpecError loading the spec, a DB outage before or during the heartbeat/authorisation
bookkeeping, an invalid run key. Tested directly, against the plain function, with no Prefect
engine or database involved: exactly what "Test the hook function directly" asked for.

Hookfix brief, round 1: `_ALERTED_IN_FLOW` now records `(flow_run.id, run_count)` for the attempt
that alerted in-body (`tuple[UUID, int] | None`), not `run_count` alone. A round-1 review caught
that Prefect 3.8.4 runs a *sync* flow body in the caller's context with no `copy_context()`
(flow_engine.py: call_with_parameters), so this `ContextVar` is shared across sequential
in-process flow runs -- keying on `run_count` alone let a value left behind by one flow run's
in-body alert wrongly suppress a *different* flow run's hook alert, since every flow run's first
attempt is run_count 1. The hook must skip only when the recorded value equals
`(flow_run.id, flow_run.run_count)` for the attempt that actually failed last.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from selector_drift.orchestrate.flows import _ALERTED_IN_FLOW, alert_on_flow_failure

_RUN_A = uuid4()
_RUN_B = uuid4()


@dataclass
class _FakeFlowRun:
    parameters: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    run_count: int = 1


@dataclass
class _FakeState:
    _exc: BaseException | None

    def result(self, raise_on_failure: bool = True) -> object:
        assert raise_on_failure is False  # the hook must never let this raise
        return self._exc


@pytest.mark.unit
def test_the_hook_posts_a_sanitised_alert_for_the_failing_target(monkeypatch) -> None:
    calls: list[tuple[str, str, str | None, str]] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )
    exc = RuntimeError("db outage\nCall log:\n  - <td>AB12345678</td>")
    flow_run = _FakeFlowRun(parameters={"spec_path": "specs/testbench.yaml"})
    alert_on_flow_failure(None, flow_run, _FakeState(exc))  # type: ignore[arg-type]
    assert calls == [("failed", "testbench", None, "RuntimeError: db outage")]


@pytest.mark.unit
def test_the_hook_uses_unknown_when_spec_path_is_missing(monkeypatch) -> None:
    calls: list[tuple[str, str, str | None, str]] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )
    flow_run = _FakeFlowRun(parameters={})
    alert_on_flow_failure(None, flow_run, _FakeState(RuntimeError("boom")))  # type: ignore[arg-type]
    assert calls == [("failed", "unknown", None, "RuntimeError: boom")]


@pytest.mark.unit
def test_the_hook_does_not_double_alert_when_the_flow_body_already_did(monkeypatch) -> None:
    """The recorded (id, run_count) matches the run and attempt that actually failed last --
    the flow body's in-body alert was for this exact failed attempt of this exact flow run."""
    _ALERTED_IN_FLOW.set((_RUN_A, 3))
    calls: list[tuple[str, str, str | None, str]] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )
    flow_run = _FakeFlowRun(
        parameters={"spec_path": "specs/testbench.yaml"}, id=_RUN_A, run_count=3
    )
    alert_on_flow_failure(None, flow_run, _FakeState(RuntimeError("boom")))  # type: ignore[arg-type]
    assert calls == []


@pytest.mark.unit
def test_the_hook_alerts_when_the_in_body_alert_was_for_an_earlier_attempt(monkeypatch) -> None:
    """Hookfix brief regression: attempt 1 of this flow run alerted in-body and recorded
    (this run's id, 1), but the attempt that actually failed last is attempt 2 of the *same* run
    (a different failure, e.g. pre-authorisation). Keying on (id, run_count) must not treat this
    as already handled."""
    _ALERTED_IN_FLOW.set((_RUN_A, 1))
    calls: list[tuple[str, str, str | None, str]] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )
    flow_run = _FakeFlowRun(
        parameters={"spec_path": "specs/testbench.yaml"}, id=_RUN_A, run_count=2
    )
    alert_on_flow_failure(None, flow_run, _FakeState(RuntimeError("boom")))  # type: ignore[arg-type]
    assert calls == [("failed", "testbench", None, "RuntimeError: boom")]


@pytest.mark.unit
def test_the_hook_alerts_when_run_count_matches_but_the_flow_run_id_does_not(monkeypatch) -> None:
    """Hookfix brief, round 1, Important finding: this is the unit-level expression of the
    cross-run leak the reviewer's probe found. `_ALERTED_IN_FLOW` records (run A's id, 1) -- run
    A's own in-body alert. The hook is now called for a *different* flow run, run B, whose
    current (and, in the real system, only) attempt is *also* run_count 1: every flow run's first
    attempt is run_count 1, so run_count alone cannot tell the two runs apart. Keying on
    (id, run_count) must still alert, because run B's id differs from run A's."""
    _ALERTED_IN_FLOW.set((_RUN_A, 1))
    calls: list[tuple[str, str, str | None, str]] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )
    flow_run = _FakeFlowRun(
        parameters={"spec_path": "specs/testbench.yaml"}, id=_RUN_B, run_count=1
    )
    alert_on_flow_failure(None, flow_run, _FakeState(RuntimeError("boom")))  # type: ignore[arg-type]
    assert calls == [("failed", "testbench", None, "RuntimeError: boom")]


@pytest.mark.unit
def test_the_hook_never_raises_even_if_post_alert_itself_fails(monkeypatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("webhook config is broken")

    monkeypatch.setattr("selector_drift.orchestrate.flows.post_alert", boom)
    flow_run = _FakeFlowRun(parameters={"spec_path": "specs/testbench.yaml"})
    alert_on_flow_failure(None, flow_run, _FakeState(RuntimeError("boom")))  # type: ignore[arg-type]  # must not raise


@pytest.mark.integration
def test_the_hook_records_an_attempt_the_body_never_settled(db, monkeypatch) -> None:
    """Task 26A: a crash or timeout the flow body's `except` never saw leaves the attempt
    `started`. The on_failure hook records it failed, with the state's message, and still
    alerts once."""
    import datetime as dt
    from pathlib import Path

    from selector_drift.config import get_settings
    from selector_drift.db.models import Run, RunAttempt
    from selector_drift.db.session import Session
    from selector_drift.orchestrate.run_record import record_attempt

    monkeypatch.setenv("SD_SPEC_DIR", str(Path(__file__).resolve().parents[2] / "specs"))
    get_settings.cache_clear()
    calls: list[tuple[str, str, str | None, str]] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )
    slot = dt.datetime(2026, 9, 7, 4, 12, tzinfo=dt.UTC)
    key = "testbench:2:2026-09-07"
    record_attempt(key, 1, "started", None, "0" * 64)

    @dataclass
    class _Timed(_FakeFlowRun):
        expected_start_time: dt.datetime | None = None

    @dataclass
    class _TimedOut(_FakeState):
        message: str = "Flow run exceeded timeout of 600.0 second(s)"

    flow_run = _Timed(parameters={"spec_path": "specs/testbench.yaml"}, expected_start_time=slot)
    alert_on_flow_failure(None, flow_run, _TimedOut(TimeoutError("timed out")))  # type: ignore[arg-type]
    with Session() as s:
        attempt = s.query(RunAttempt).filter_by(run_key=key, attempt=1).one()
        run = s.get(Run, key)
    assert (attempt.state, attempt.error) == ("failed", _TimedOut.message)
    assert attempt.finished_at is not None and run is not None and run.state == "failed"
    assert [c[0] for c in calls] == ["failed"]
    get_settings.cache_clear()
