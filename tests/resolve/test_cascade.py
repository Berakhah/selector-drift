from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from selector_drift.resolve import cascade
from selector_drift.resolve.cache import NO_CACHE
from selector_drift.resolve.cascade import parse_run_key, resolve_cascade
from selector_drift.resolve.escalation import EscalateToHuman
from selector_drift.resolve.page import PageSource, StaticPageSource
from selector_drift.resolve.trace import (
    AnchorEvidence,
    CascadeTrace,
    Family,
    Miss,
    ResolvedElement,
    Rung,
    RungAttempt,
)
from selector_drift.spec.schema import FieldTarget
from tests.resolve.conftest import CLAIM_NUMBER, RUN_KEY

PAGE = """<html><body><table>
<tr><th>Claim Number</th></tr>
<tr><td id="real">AB12345678</td></tr>
<tr><td id="ghost" style="display:none">ZZ00000000</td></tr>
</table></body></html>"""


@dataclass
class FakeRung:
    rung_id: int
    family: Family
    answer: str | None  # a CSS selector this rung "resolves" to, or None
    calls: list[int] = field(default_factory=list)

    def resolve(self, page: PageSource, target: FieldTarget) -> ResolvedElement | None:
        self.calls.append(self.rung_id)
        if self.answer is None:
            return None
        el = page.query(self.answer)[0]
        return ResolvedElement(
            selector=self.answer,
            node_id=page.node_id(el),
            anchor=AnchorEvidence(accessible_name="Claim Number"),
            rung_id=self.rung_id,
        )


@dataclass
class DecliningRung(FakeRung):
    def attempt(self, page: PageSource, target: FieldTarget) -> ResolvedElement | Miss:
        self.calls.append(self.rung_id)
        return Miss("ambiguous", "two candidates")


@dataclass
class EscalatingRung(FakeRung):
    def escalate(
        self,
        page: PageSource,
        target: FieldTarget,
        *,
        run_key: str | None,
        attempts: tuple[RungAttempt, ...],
    ) -> ResolvedElement:
        self.calls.append(self.rung_id)
        raise EscalateToHuman("escalated", case=Path("case.json"))


@dataclass
class SpyRecorder:
    rows: list[CascadeTrace] = field(default_factory=list)

    def __call__(
        self,
        *,
        run_key: str,
        portal: str,
        target: FieldTarget,
        trace: CascadeTrace,
        node_id: str | None,
        ms: int,
    ) -> int:
        assert (run_key, portal) == (RUN_KEY, "testbench")
        self.rows.append(trace)
        return 41 + len(self.rows)


@pytest.fixture
def use(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[Rung]]:
    def install(*rungs: Rung) -> list[Rung]:
        monkeypatch.setattr(cascade, "REGISTERED_RUNGS", list(rungs))
        return list(rungs)

    return install


@pytest.fixture
def page() -> StaticPageSource:
    return StaticPageSource(PAGE)


@pytest.mark.unit
def test_cascade_tries_rungs_in_order_and_records_every_attempt(use, page) -> None:
    use(FakeRung(1, Family.NONE, None), FakeRung(2, Family.TEXT_SEMANTIC, "#real"))
    el, trace = resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, record=False)
    assert [(a.rung, a.family, a.outcome) for a in trace.attempts] == [
        (1, Family.NONE, "no_match"),
        (2, Family.TEXT_SEMANTIC, "resolved"),
    ]
    assert trace.winner == 2 and el.rung_id == 2


@pytest.mark.unit
def test_the_first_winner_stops_the_cascade(use, page) -> None:
    _first, second = use(
        FakeRung(2, Family.TEXT_SEMANTIC, "#real"), FakeRung(3, Family.TEXT_SEMANTIC, "#real")
    )
    resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, record=False)
    assert isinstance(second, FakeRung) and second.calls == []


@pytest.mark.unit
@pytest.mark.parametrize(("winner", "healed"), [(1, False), (2, True), (4, True)])
def test_healed_is_false_only_on_a_rung_one_win(use, page, winner: int, healed: bool) -> None:
    use(FakeRung(winner, Family.NONE, "#real"))
    _, trace = resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, record=False)
    assert trace.winner == winner and trace.healed is healed


@pytest.mark.unit
def test_every_attempt_is_timed_in_whole_milliseconds(use, page) -> None:
    use(FakeRung(1, Family.NONE, None), FakeRung(2, Family.TEXT_SEMANTIC, "#real"))
    _, trace = resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, record=False)
    assert all(isinstance(a.ms, int) and a.ms >= 0 for a in trace.attempts)


@pytest.mark.unit
def test_a_rungs_reason_for_declining_is_recorded(use, page) -> None:
    use(DecliningRung(3, Family.TEXT_SEMANTIC, None), FakeRung(4, Family.STRUCTURAL, "#real"))
    _, trace = resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, record=False)
    assert trace.attempts[0].outcome == "ambiguous"


@pytest.mark.unit
def test_a_hidden_element_is_never_accepted_whatever_the_rung_says(use, page) -> None:
    """H9, enforced by the runner as well as by every rung."""
    use(FakeRung(2, Family.TEXT_SEMANTIC, "#ghost"), FakeRung(4, Family.STRUCTURAL, "#real"))
    el, trace = resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, record=False)
    assert trace.attempts[0].outcome == "not_visible"
    assert page.visible(page.query("#real")[0]) and el.node_id == page.node_id(
        page.query("#real")[0]
    )


@pytest.mark.unit
def test_no_winner_escalates_and_carries_the_trace(use, page) -> None:
    use(FakeRung(2, Family.TEXT_SEMANTIC, None))
    with pytest.raises(EscalateToHuman) as caught:
        resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, record=False)
    assert caught.value.trace is not None and caught.value.trace.winner is None


@pytest.mark.unit
def test_replay_writes_nothing_and_never_runs_rung_five(use, page) -> None:
    """Task 19: record=False is replay and the suites -- not the published denominator."""
    spy = SpyRecorder()
    _, _, five = use(
        FakeRung(2, Family.TEXT_SEMANTIC, None),
        FakeRung(4, Family.STRUCTURAL, None),
        EscalatingRung(5, Family.NONE, None),
    )
    with pytest.raises(EscalateToHuman):
        resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, record=False, recorder=spy)
    assert spy.rows == [] and isinstance(five, FakeRung) and five.calls == []


@pytest.mark.unit
def test_a_scheduled_resolution_is_recorded_once_on_a_win(use, page) -> None:
    spy = SpyRecorder()
    use(FakeRung(2, Family.TEXT_SEMANTIC, "#real"))
    _, trace = resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, run_key=RUN_KEY, recorder=spy)
    assert len(spy.rows) == 1 and trace.resolution_id == 42


@pytest.mark.unit
def test_a_scheduled_resolution_is_recorded_once_on_an_escalation(use, page) -> None:
    """C4: the denominator counts the losses too."""
    spy = SpyRecorder()
    use(FakeRung(2, Family.TEXT_SEMANTIC, None), EscalatingRung(5, Family.NONE, None))
    with pytest.raises(EscalateToHuman) as caught:
        resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, run_key=RUN_KEY, recorder=spy)
    assert len(spy.rows) == 1 and spy.rows[0].winner is None
    trace = caught.value.trace
    assert trace is not None and trace.resolution_id == 42
    assert trace.attempts[-1].outcome == "escalated" and caught.value.case == Path("case.json")


@pytest.mark.unit
def test_a_scheduled_resolution_without_a_run_key_is_refused(use, page) -> None:
    use(FakeRung(2, Family.TEXT_SEMANTIC, "#real"))
    with pytest.raises(ValueError, match="run_key"):
        resolve_cascade(page, CLAIM_NUMBER, NO_CACHE)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["testbench", "testbench:v2:2026-10-01", ":2:2026-10-01"])
def test_a_malformed_run_key_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_run_key(bad)


@pytest.mark.unit
def test_an_attempt_serialises_to_the_frozen_shape() -> None:
    """resolutions.attempts and heal_events.trace are published via RungAttemptView."""
    assert RungAttempt(2, Family.TEXT_SEMANTIC, "resolved", 3).as_json() == {
        "rung": 2,
        "family": "TEXT_SEMANTIC",
        "outcome": "resolved",
        "ms": 3,
    }


@pytest.mark.unit
def test_registered_rungs_run_cheapest_first() -> None:
    ids = [r.rung_id for r in cascade.REGISTERED_RUNGS]
    assert ids == sorted(set(ids))
