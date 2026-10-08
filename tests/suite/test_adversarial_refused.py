import pytest

from selector_drift.validate.quarantine import QuarantinedRun
from selector_drift.validate.verdict import Outcome
from tests.suite.conftest import ALL_ADVERSARIAL_MUTATION_CLASSES, judge_run

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
    """THE invariant (2026-09-26): quarantined, or every field on a true node. Checked both on
    run one (no history) and on a warmed-up target."""
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
