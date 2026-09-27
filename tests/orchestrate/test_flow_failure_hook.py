"""Plus item (final review): `alert_on_flow_failure` is the Prefect `on_failure` hook that
covers the failure paths `extraction_flow`'s own `except Exception` block never reaches --
a SpecError loading the spec, a DB outage before or during the heartbeat/authorisation
bookkeeping, an invalid run key. Tested directly, against the plain function, with no Prefect
engine or database involved: exactly what "Test the hook function directly" asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from selector_drift.orchestrate.flows import _ALERTED_IN_FLOW, alert_on_flow_failure


@dataclass
class _FakeFlowRun:
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeState:
    _exc: BaseException | None

    def result(self, raise_on_failure: bool = True) -> object:
        assert raise_on_failure is False  # the hook must never let this raise
        return self._exc


@pytest.fixture(autouse=True)
def _reset_alerted_marker() -> None:
    """Every test starts as though the flow body never reached its own alert branch."""
    _ALERTED_IN_FLOW.set(False)


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
    _ALERTED_IN_FLOW.set(True)
    calls: list[tuple[str, str, str | None, str]] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )
    flow_run = _FakeFlowRun(parameters={"spec_path": "specs/testbench.yaml"})
    alert_on_flow_failure(None, flow_run, _FakeState(RuntimeError("boom")))  # type: ignore[arg-type]
    assert calls == []


@pytest.mark.unit
def test_the_hook_never_raises_even_if_post_alert_itself_fails(monkeypatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("webhook config is broken")

    monkeypatch.setattr("selector_drift.orchestrate.flows.post_alert", boom)
    flow_run = _FakeFlowRun(parameters={"spec_path": "specs/testbench.yaml"})
    alert_on_flow_failure(None, flow_run, _FakeState(RuntimeError("boom")))  # type: ignore[arg-type]  # must not raise
