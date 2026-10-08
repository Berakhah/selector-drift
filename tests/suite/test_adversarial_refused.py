import pytest
from bok_core.boundary.sensitivity import Sensitivity

from selector_drift.heallog.capture import Evidence
from selector_drift.heallog.replay import replay
from selector_drift.resolve import cascade
from selector_drift.resolve.page import PageSource
from selector_drift.resolve.rungs.r1_cache import CacheRung
from selector_drift.resolve.rungs.r2_axtree import AxTreeRung
from selector_drift.resolve.rungs.r3_anchors import AnchorRung
from selector_drift.resolve.rungs.r4_heuristics import HeuristicRung
from selector_drift.resolve.trace import AnchorEvidence, Family, ResolvedElement
from selector_drift.spec.schema import FieldTarget
from selector_drift.validate.gate import ValidatedExtraction
from selector_drift.validate.quarantine import QuarantinedRun
from selector_drift.validate.verdict import Outcome
from testbench.portal.render import render_results
from tests.suite.conftest import (
    ALL_ADVERSARIAL_MUTATION_CLASSES,
    AMOUNT,
    AT,
    BASE_DAY,
    RUN_KEY,
    judge_run,
)

RESOLVER_ATTACKS = [e for e in ALL_ADVERSARIAL_MUTATION_CLASSES if e.name != "VirtualisedRows"]


def _failed(result, field: str, checks: set[str]) -> bool:
    return any(
        v.field == field and v.check in checks and v.outcome is Outcome.FAIL
        for v in result.verdicts
    )


@pytest.mark.unit
@pytest.mark.parametrize("m", RESOLVER_ATTACKS, ids=lambda m: m.name)
def test_no_adversarial_class_ever_writes_a_wrong_value(
    m, bench_evidence, truth, history, warm_cache
):
    """THE invariant (2026-09-26): no write from a wrong node -- quarantined, or every field on
    a true node. Checked both on run one (no history) and on a warmed-up target. It judges node
    identity only: a value attack on the right node (ValueSwap) is pinned separately below."""
    for hist in (None, history):
        elements, result = judge_run(bench_evidence[m.name], history=hist, cache=warm_cache())
        wrong = sorted(f for f, el in elements.items() if el.node_id not in truth[m.name][f])
        written = result is not None and not isinstance(result, QuarantinedRun)
        assert not (written and wrong), (
            f"{m.name} (history={'yes' if hist else 'none'}) would write {wrong} "
            "from cells outside the answer key"
        )


@pytest.mark.unit
def test_value_swap_is_refused_on_a_cache_hit_by_the_collapse_floor(
    bench_evidence, history, warm_cache
):
    """Markup untouched, so rung 1 hits and nothing heals (the realistic context). Only
    RowKeyContinuity's steady-state collapse floor (Task 22, ledger #32) can see it."""
    _, result = judge_run(bench_evidence["ValueSwap"], history=history, cache=warm_cache())
    assert isinstance(result, QuarantinedRun)
    assert _failed(result, "amount", {"RowKeyContinuity"})


@pytest.mark.unit
def test_locale_flip_is_refused_by_the_format_or_domain_check(bench_evidence, history, warm_cache):
    """A FAIL quarantines whether or not a heal happened (design 5.7)."""
    _, result = judge_run(bench_evidence["LocaleFlip"], history=history, cache=warm_cache())
    assert isinstance(result, QuarantinedRun)
    assert _failed(result, "amount", {"FormatAssertion", "DomainAssertion"})


@pytest.mark.unit
def test_a_decoy_heal_off_the_true_column_is_refused_by_a_confirmer(
    decoy_evidence, truth, history, warm_cache
):
    """The plausible-but-wrong heal. If the resolver lands on the decoy, a confirming family
    must say so -- on run one with no history (the DOM split) and with history (the values)."""
    for hist in (None, history):
        elements, result = judge_run(decoy_evidence, history=hist, cache=warm_cache())
        el = elements.get("amount")
        if el is None or el.node_id in truth["DecoyColumn"]["amount"]:
            continue  # not fooled: the invariant above already holds
        assert isinstance(result, QuarantinedRun)
        assert _failed(result, "amount", {"IndependentRungAgreement", "RowKeyContinuity"})


class _OffByOneColumnRung:
    """A rung-4 heuristic with the adjacent-column bug: it trusts the column POSITION Amount had
    on the clean page, so when DecoyColumn inserts "Prior Amount" in that position it lands on
    the decoy cell -- the plausible-but-wrong heal the gate exists to refuse. Resolves `amount`
    only; every other field it declines. The real cascade is never fooled (rung 2 heals to the
    true column), so this stand-in is what makes the gate's refusal branch reachable."""

    rung_id = 4
    family = Family.STRUCTURAL

    def resolve(self, page: PageSource, target: FieldTarget) -> ResolvedElement | None:
        if target.name != "amount":
            return None
        true_cell = AxTreeRung().resolve(page, target)
        if true_cell is None:
            return None
        node = page.node(true_cell.node_id)
        decoy = node.getprevious() if node is not None else None
        if decoy is None:
            return None
        return ResolvedElement(
            selector="td.amount-by-position",
            node_id=page.node_id(decoy),
            anchor=AnchorEvidence(header_text="Amount"),
            rung_id=4,
        )


@pytest.mark.unit
def test_a_rung_that_lands_on_the_decoy_is_refused_by_a_confirmer(
    decoy_evidence, truth, history, warm_cache, monkeypatch
):
    """The fooled branch, made reachable: the off-by-one rung is placed in the cascade where it
    wins for `amount`, and the SAME instance is injected into IndependentRungAgreement as the
    rung-4 healer, so the gate re-derives the heal and re-resolves with the independent rungs
    2 and 3. On run one (no history) and with history, a confirmer must FAIL it."""
    fooled = _OffByOneColumnRung()
    monkeypatch.setattr(
        cascade,
        "REGISTERED_RUNGS",
        [CacheRung(), fooled, AxTreeRung(), AnchorRung(), HeuristicRung()],
    )
    confirmers = [AxTreeRung(), AnchorRung(), fooled]

    el, trace = replay(decoy_evidence, AMOUNT, warm_cache())
    assert trace.winner == 4 and trace.healed
    assert el.node_id not in truth["DecoyColumn"]["amount"], "the stub must land off the column"

    for hist in (None, history):
        elements, result = judge_run(
            decoy_evidence, history=hist, cache=warm_cache(), confirmers=confirmers
        )
        assert elements["amount"].node_id == el.node_id  # judged the fooled heal, no skip
        assert isinstance(result, QuarantinedRun), f"history={'yes' if hist else 'none'}"
        assert _failed(result, "amount", {"IndependentRungAgreement", "RowKeyContinuity"}), [
            (v.check, v.outcome, v.reason) for v in result.verdicts if v.field == "amount"
        ]


@pytest.mark.unit
def test_value_swap_is_written_on_run_one_and_refused_once_there_is_history(
    bench_evidence, truth, history, warm_cache
):
    """The known limit (Task 26 fix round 1, ruling I2(b)): a value attack on the RIGHT nodes
    has no VALUE-family basis without a prior accepted run, so run one writes it. From the
    first run with history, RowKeyContinuity refuses it."""
    elements, first = judge_run(bench_evidence["ValueSwap"], history=None, cache=warm_cache())
    assert all(el.node_id in truth["ValueSwap"][f] for f, el in elements.items())
    assert isinstance(first, ValidatedExtraction)

    _, warmed = judge_run(bench_evidence["ValueSwap"], history=history, cache=warm_cache())
    assert isinstance(warmed, QuarantinedRun)
    assert _failed(warmed, "amount", {"RowKeyContinuity"})


@pytest.mark.unit
def test_positive_control_a_clean_page_is_written(history, warm_cache):
    """Without this, a gate that quarantined everything would pass every test above."""
    clean = Evidence.from_static(
        render_results(BASE_DAY),
        run_key=RUN_KEY,
        sensitivity=Sensitivity.SYNTHETIC,
        captured_at=AT,
    )
    for hist in (None, history):
        _, result = judge_run(clean, history=hist, cache=warm_cache())
        assert isinstance(result, ValidatedExtraction), f"history={'yes' if hist else 'none'}"
        assert not [v for v in result.verdicts if v.outcome is Outcome.FAIL]
