"""A heal that cannot be replayed from what was stored does not reach the lake or the cache.

CLAUDE.md: "A heal you cannot reproduce is an anecdote." `bundle_and_replay` used to record
`replayed_ok=False` and `settle` then accepted the run anyway, so the flag was a note nobody read.
It is now a failing verdict and the run quarantines, the same route a redaction failure takes.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from selector_drift.orchestrate import accept as accept_mod
from selector_drift.validate.gate import ValidatedExtraction
from selector_drift.validate.verdict import Outcome, Verdict


def _passing_verdict() -> Verdict:
    return Verdict(check="Format", outcome=Outcome.PASS, threshold=None, observed=None, reason="ok")


@pytest.fixture
def seams(monkeypatch):
    calls = SimpleNamespace(accepted=[], held=[])
    monkeypatch.setattr(accept_mod, "redact_evidence", lambda _ev, key: object())
    monkeypatch.setattr(accept_mod, "accept", lambda *a, **k: calls.accepted.append((a, k)))
    monkeypatch.setattr(accept_mod, "hold", lambda *a, **k: calls.held.append((a, k)) or "uri")
    return calls


def _settle(replay):
    """settle() over one validated run; `replay(heal)` says whether that heal reproduced."""
    heals = [SimpleNamespace(field=f) for f in replay.fields]
    result = MagicMock(spec=ValidatedExtraction)
    result.verdicts = [_passing_verdict()]
    return accept_mod.settle(
        target=SimpleNamespace(target="t"),
        spec=SimpleNamespace(targets=[SimpleNamespace(name=h.field) for h in heals]),
        candidate=SimpleNamespace(run_key="t:2026-01-01"),
        result=result,
        evidence=object(),
        elements={h.field: SimpleNamespace(node_id="n", rung_id=2) for h in heals},
        heals=heals,
        cache=object(),
        cache_before={},
        redaction_key=b"k",
    )


class _Replay:
    """Stands in for bundle_and_replay: records the order and answers per field."""

    def __init__(self, outcomes: dict[str, bool]) -> None:
        self.outcomes = outcomes
        self.fields = list(outcomes)
        self.seen: list[str] = []

    def __call__(self, heal, *_a, **_k):
        self.seen.append(heal.field)
        return self.outcomes[heal.field]


@pytest.mark.unit
def test_a_heal_that_does_not_replay_quarantines_and_writes_nothing(seams, monkeypatch):
    replay = _Replay({"f0": False})
    monkeypatch.setattr(accept_mod, "bundle_and_replay", replay)

    assert _settle(replay) == "quarantined"

    assert seams.accepted == []
    ((run_key, reason, verdicts, _candidate), _kw) = seams.held[0]
    assert run_key == "t:2026-01-01"
    failing = [v for v in verdicts if v.outcome is Outcome.FAIL]
    assert [v.check for v in failing] == ["HealReproducibility"]
    assert "replay" in reason
    assert any(v.check == "Format" for v in verdicts), "the gate's own verdicts stay with the run"


@pytest.mark.unit
def test_one_unreproducible_heal_among_several_is_enough_to_quarantine(seams, monkeypatch):
    replay = _Replay({"f0": True, "f1": False, "f2": True})
    monkeypatch.setattr(accept_mod, "bundle_and_replay", replay)

    assert _settle(replay) == "quarantined"

    assert seams.accepted == []
    # Quarantine is the run's outcome, not a reason to skip the evidence of the heals after it.
    assert replay.seen == ["f0", "f1", "f2"]


@pytest.mark.unit
def test_replayable_heals_are_still_accepted(seams, monkeypatch):
    replay = _Replay({"f0": True, "f1": True})
    monkeypatch.setattr(accept_mod, "bundle_and_replay", replay)

    assert _settle(replay) == "healed"

    assert len(seams.accepted) == 1
    assert seams.held == []
