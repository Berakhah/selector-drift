"""The two decisions a human can take on a quarantined run (Task 26A Part 2, Task 29A wiring).

The record follows the data (ledger 2026-09-26): re-gate, land, THEN mark promoted and record
the intervention -- a lake failure in between leaves the quarantine open and retryable."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from selector_drift.orchestrate import quarantine_actions as qa

RUN_KEY = "testbench:2:2026-10-01"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    log: list[str] = []
    validated = SimpleNamespace(run_key=RUN_KEY, verdicts=())
    monkeypatch.setattr(
        qa.quarantine, "promote", lambda *a, **k: (log.append("promote"), validated)[1]
    )
    monkeypatch.setattr(
        qa, "accept_promoted", lambda *a, **k: (log.append("land"), Path("/lake/x.parquet"))[1]
    )
    monkeypatch.setattr(qa.quarantine, "mark_promoted", lambda *a, **k: log.append("mark"))
    monkeypatch.setattr(qa, "_mark_heals_promoted", lambda *a, **k: log.append("heals"))
    monkeypatch.setattr(qa, "record_intervention", lambda *a, **k: log.append("intervention"))
    monkeypatch.setattr(qa.CacheWriter, "for_run_key", classmethod(lambda cls, key: None))
    return log


@pytest.mark.unit
def test_promotion_is_recorded_only_after_the_data_lands(
    calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    qa.build_quarantine_actions().promote(RUN_KEY, "op1", "checked the screenshots")
    assert calls == ["promote", "land", "mark", "heals", "intervention"]

    calls.clear()

    def lake_down(*a: object, **k: object) -> None:
        raise OSError("lake unavailable")

    monkeypatch.setattr(qa, "accept_promoted", lake_down)
    with pytest.raises(OSError):
        qa.build_quarantine_actions().promote(RUN_KEY, "op1", "checked the screenshots")
    assert calls == ["promote"]  # never marked, never an intervention


@pytest.mark.unit
def test_a_manual_promote_is_recorded_as_an_intervention(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 29A's dropped test, landed with quarantine_actions.py."""
    recorded: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        qa.quarantine,
        "promote",
        lambda run_key, **k: SimpleNamespace(run_key=run_key, verdicts=()),
    )
    monkeypatch.setattr(
        qa, "accept_promoted", lambda run_key, validated, *, cache: Path("/lake/x.parquet")
    )
    monkeypatch.setattr(qa.quarantine, "mark_promoted", lambda *a, **k: None)
    monkeypatch.setattr(qa, "_mark_heals_promoted", lambda *a, **k: None)
    monkeypatch.setattr(qa.CacheWriter, "for_run_key", classmethod(lambda cls, run_key: object()))
    monkeypatch.setattr(qa, "record_intervention", lambda *a, **k: recorded.append(a) or 1)
    qa.build_quarantine_actions().promote("testbench:2:2026-10-01", "op1", "values checked")
    ((kind, actor, target, *_rest),) = recorded
    assert (kind, actor, target) == ("manual_promote", "op1", "testbench")


@pytest.mark.unit
def test_a_promotion_regated_for_another_run_is_refused_before_anything_lands(
    calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 25 minor, taken in 26A: mark_promoted trusts any verdict list, so the validated
    extraction must be checked to be THIS run's before anything is written."""
    monkeypatch.setattr(
        qa.quarantine,
        "promote",
        lambda *a, **k: (calls.append("promote"), SimpleNamespace(run_key="other:2:2026-10-01"))[1],
    )
    with pytest.raises(ValueError, match="another run"):
        qa.build_quarantine_actions().promote(RUN_KEY, "op1", "checked")
    assert calls == ["promote"]


@pytest.mark.unit
def test_discard_delegates_to_the_audited_quarantine_discard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        qa.quarantine,
        "discard",
        lambda run_key, *, actor, reason: seen.append((run_key, actor, reason)),
    )
    qa.build_quarantine_actions().discard(RUN_KEY, "op1", "a real portal change, re-run")
    assert seen == [(RUN_KEY, "op1", "a real portal change, re-run")]
