from __future__ import annotations

import pytest

from selector_drift.extract.models import CandidateExtraction
from selector_drift.resolve.page import PageSource
from selector_drift.resolve.trace import Family, ResolvedElement
from selector_drift.spec.schema import FieldTarget
from selector_drift.validate.assertions.base import Assertion
from selector_drift.validate.assertions.independence import (
    COLLAPSE_FLOOR,
    MIN_JOINED,
    IndependentRungAgreement,
    RowKeyContinuity,
    continuity_alert,
)
from selector_drift.validate.gate import HealContext, QuarantinedRun, ValidatedExtraction, gate_run
from selector_drift.validate.history import AcceptedRun, History
from selector_drift.validate.verdict import Confirmation, Outcome, Verdict
from tests.validate.helpers import (
    AMOUNT,
    AMOUNT_CELL,
    CHURNED,
    CLAIM_NUMBER,
    DECOY,
    FOREIGN_KEYS,
    PRIOR_AMOUNT_CELL,
    RESERVE,
    SWAPPED,
    UNCHANGED,
    FakeRung,
    anchors,
    ax,
    candidate,
    ctx,
    heuristic,
    previous_history,
)


def healed(rung: int, **kw: object) -> HealContext:
    return ctx(AMOUNT, healed=True, rung=rung, **kw)  # type: ignore[arg-type]


# ---- DOM families --------------------------------------------------------------------------


@pytest.mark.unit
def test_same_family_confirmer_is_rejected() -> None:
    """Rungs 2 and 3 both key off text, so a label reword fools both identically.
    Their agreement proves nothing and must not be accepted as confirmation."""
    v = IndependentRungAgreement([ax(AMOUNT_CELL), anchors(AMOUNT_CELL)]).check(
        UNCHANGED, None, healed(3)
    )
    assert v.outcome is Outcome.INSUFFICIENT and "cross-family" in v.reason


@pytest.mark.unit
def test_cross_family_agreement_passes() -> None:
    v = IndependentRungAgreement([ax(None), anchors(AMOUNT_CELL), heuristic(AMOUNT_CELL)]).check(
        UNCHANGED, None, healed(3)
    )
    assert v.outcome is Outcome.PASS and v.observed == "rung 4"


@pytest.mark.unit
def test_a_heal_that_extracted_another_node_than_its_rederivation_fails() -> None:
    """Ledger 551 (Task 26A): the confirmers agree with the healer's RE-DERIVATION, but the run
    extracted the decoy cell. Comparing the confirmers with each other alone passed this."""
    rungs = [ax(AMOUNT_CELL), heuristic(AMOUNT_CELL)]
    v = IndependentRungAgreement(rungs).check(
        UNCHANGED, previous_history(), healed(2, node=PRIOR_AMOUNT_CELL)
    )
    assert v.outcome is Outcome.FAIL and "extracted a different node" in v.reason
    assert v.observed == f"extracted {PRIOR_AMOUNT_CELL}; rung 2 -> {AMOUNT_CELL}"


@pytest.mark.unit
def test_the_extracted_node_is_compared_even_with_no_cross_family_confirmer() -> None:
    """Without a confirmer the VALUE family would decide -- and values read from the right cell
    could vouch for a heal that extracted the wrong one."""
    v = IndependentRungAgreement([ax(AMOUNT_CELL)]).check(
        UNCHANGED, previous_history(), healed(2, node=PRIOR_AMOUNT_CELL)
    )
    assert v.outcome is Outcome.FAIL and "extracted a different node" in v.reason


@pytest.mark.unit
def test_a_heal_that_extracted_its_rederived_node_is_confirmed_as_before() -> None:
    v = IndependentRungAgreement([ax(AMOUNT_CELL), heuristic(AMOUNT_CELL)]).check(
        UNCHANGED, previous_history(), healed(2, node=AMOUNT_CELL)
    )
    assert v.outcome is Outcome.PASS


@pytest.mark.unit
def test_a_passing_confirmation_names_its_confirmer_in_structure() -> None:
    v = IndependentRungAgreement([ax(AMOUNT_CELL), heuristic(AMOUNT_CELL)]).check(
        UNCHANGED, previous_history(), healed(2)
    )
    assert v.outcome is Outcome.PASS
    assert v.confirmed_by == Confirmation(rung=4, family=Family.STRUCTURAL)


@pytest.mark.unit
def test_cross_family_disagreement_fails_on_run_one_with_no_history() -> None:
    """The decoy column: rung 4 healed to Prior Amount, rung 2 points at Amount."""
    v = IndependentRungAgreement([ax(AMOUNT_CELL), heuristic(PRIOR_AMOUNT_CELL)]).check(
        DECOY, None, healed(4)
    )
    assert v.outcome is Outcome.FAIL and v.confirmed_by is None
    assert "disagreement" in v.reason


@pytest.mark.unit
def test_one_disagreeing_confirmer_outweighs_an_agreeing_one() -> None:
    v = IndependentRungAgreement(
        [ax(AMOUNT_CELL), anchors(PRIOR_AMOUNT_CELL), heuristic(AMOUNT_CELL)]
    ).check(UNCHANGED, None, healed(4))
    assert v.outcome is Outcome.FAIL


@pytest.mark.unit
def test_no_confirmer_available_is_insufficient_never_pass() -> None:
    v = IndependentRungAgreement([heuristic(AMOUNT_CELL)]).check(UNCHANGED, None, healed(4))
    assert v.outcome is Outcome.INSUFFICIENT


@pytest.mark.unit
def test_rung_one_is_never_used_as_a_confirmer_even_if_mislabelled() -> None:
    cache_rung = FakeRung(1, Family.TEXT_SEMANTIC, PRIOR_AMOUNT_CELL)  # would disagree
    v = IndependentRungAgreement([cache_rung, heuristic(AMOUNT_CELL)]).check(
        UNCHANGED, None, healed(4)
    )
    assert v.outcome is Outcome.INSUFFICIENT and v.observed != "rung 1"


@pytest.mark.unit
def test_agreement_compares_node_identity_not_selector_text() -> None:
    # R10/C5. Different selector strings, same node: agreement.
    v = IndependentRungAgreement(
        [ax(AMOUNT_CELL, selector="#amount"), heuristic(AMOUNT_CELL, selector="td:nth-child(3)")]
    ).check(UNCHANGED, None, healed(2))
    assert v.outcome is Outcome.PASS


@pytest.mark.unit
def test_the_same_selector_text_naming_different_nodes_is_disagreement() -> None:
    v = IndependentRungAgreement(
        [ax(AMOUNT_CELL, selector="td.value"), heuristic(PRIOR_AMOUNT_CELL, selector="td.value")]
    ).check(UNCHANGED, None, healed(2))
    assert v.outcome is Outcome.FAIL


@pytest.mark.unit
def test_a_heal_the_healing_rung_cannot_reproduce_is_a_failure() -> None:
    v = IndependentRungAgreement([ax(AMOUNT_CELL), heuristic(None)]).check(
        UNCHANGED, None, healed(4)
    )
    assert v.outcome is Outcome.FAIL and "not reproducible" in v.reason


@pytest.mark.unit
def test_nothing_healed_means_nothing_to_confirm() -> None:
    v = IndependentRungAgreement([ax(AMOUNT_CELL), heuristic(AMOUNT_CELL)]).check(
        UNCHANGED, previous_history(), ctx(AMOUNT, healed=False, rung=1)
    )
    assert v.outcome is Outcome.INSUFFICIENT and v.confirmed_by is None


# ---- the VALUE family (R14/C3) -------------------------------------------------------------


@pytest.mark.unit
def test_a_label_reword_is_confirmed_by_value_continuity_and_is_not_quarantined() -> None:
    # R14/C3. Both text rungs fail on a reword, so without a VALUE confirmer this
    # quarantines forever -- and it is the most common real UI change there is.
    v = IndependentRungAgreement([ax(None), anchors(None), heuristic(AMOUNT_CELL)]).check(
        UNCHANGED, previous_history(), healed(4)
    )
    assert v.outcome is Outcome.PASS and v.observed == "row-key continuity"
    assert v.confirmed_by == Confirmation(rung=None, family=Family.VALUE)


@pytest.mark.unit
def test_with_no_page_the_value_family_is_the_only_confirmer() -> None:
    v = IndependentRungAgreement([ax(AMOUNT_CELL), heuristic(AMOUNT_CELL)]).check(
        UNCHANGED, previous_history(), healed(4, page=None)
    )
    assert v.outcome is Outcome.PASS and v.confirmed_by is not None
    assert v.confirmed_by.family is Family.VALUE
    assert "no page snapshot" in v.reason


@pytest.mark.unit
def test_a_value_confirmation_has_no_rung() -> None:
    v = RowKeyContinuity().check(UNCHANGED, previous_history(), healed(4))
    assert v.outcome is Outcome.PASS and v.threshold == ">= 0.95"
    assert v.confirmed_by == Confirmation(rung=None, family=Family.VALUE)


@pytest.mark.unit
def test_the_decoy_column_is_refused_by_value_continuity_too() -> None:
    v = RowKeyContinuity().check(DECOY, previous_history(), healed(4))
    assert v.outcome is Outcome.FAIL and v.observed is not None
    assert float(v.observed) < 0.95 and v.confirmed_by is None


@pytest.mark.unit
def test_value_swap_is_caught_by_nothing_else() -> None:
    # Task 11A's ValueSwap leaves markup untouched, so every DOM signal agrees.
    dom = IndependentRungAgreement([ax(AMOUNT_CELL), heuristic(AMOUNT_CELL)]).check(
        SWAPPED, previous_history(), healed(4)
    )
    assert dom.outcome is Outcome.PASS
    assert RowKeyContinuity().check(SWAPPED, previous_history(), healed(4)).outcome is Outcome.FAIL


@pytest.mark.unit
def test_continuity_with_no_history_is_insufficient_never_pass() -> None:
    assert RowKeyContinuity().check(UNCHANGED, None, healed(4)).outcome is Outcome.INSUFFICIENT


@pytest.mark.unit
def test_steady_state_churn_is_tolerated_but_alerted() -> None:
    # Ledger #32 (2026-09-26). Values legitimately change between days: 0.90 agreement on a
    # cache hit is INSUFFICIENT (tolerated), and continuity_alert() tells the pipeline to report it.
    v = RowKeyContinuity().check(CHURNED, previous_history(), ctx(AMOUNT, healed=False, rung=1))
    assert v.outcome is Outcome.INSUFFICIENT and v.observed == "0.9000"
    assert continuity_alert(v)


@pytest.mark.unit
def test_steady_state_value_swap_is_a_collapse_and_fails() -> None:
    # Ledger #32. ValueSwap under unchanged markup: rung 1 hits, nothing heals, and without
    # this FAIL the swapped column is written every night. Agreement 0.0 < COLLAPSE_FLOOR.
    v = RowKeyContinuity().check(SWAPPED, previous_history(), ctx(AMOUNT, healed=False, rung=1))
    assert v.outcome is Outcome.FAIL and v.observed is not None
    assert float(v.observed) < COLLAPSE_FLOOR == 0.50
    assert "collapse" in v.reason and not continuity_alert(v)


@pytest.mark.unit
def test_an_unchanged_steady_state_run_is_neither_blocked_nor_alerted() -> None:
    v = RowKeyContinuity().check(UNCHANGED, previous_history(), ctx(AMOUNT, healed=False, rung=1))
    assert v.outcome is Outcome.INSUFFICIENT and not continuity_alert(v)


@pytest.mark.unit
def test_on_a_heal_the_same_churn_is_a_failure() -> None:
    """Between the floor and the threshold, a heal is refused: the tolerance is for cache hits."""
    v = RowKeyContinuity().check(CHURNED, previous_history(), healed(4))
    assert v.outcome is Outcome.FAIL


@pytest.mark.unit
def test_a_healed_key_field_is_judged_by_the_fields_that_co_vary_with_it() -> None:
    v = RowKeyContinuity().check(UNCHANGED, previous_history(), ctx(CLAIM_NUMBER, rung=4))
    assert v.outcome is Outcome.PASS and v.observed == "1.0000"


@pytest.mark.unit
def test_a_key_heal_onto_foreign_values_joins_nothing_and_is_not_confirmed() -> None:
    v = RowKeyContinuity().check(FOREIGN_KEYS, previous_history(), ctx(CLAIM_NUMBER, rung=4))
    assert v.outcome is Outcome.INSUFFICIENT and "no natural keys in common" in v.reason


# ---- beyond the brief: edges the gate depends on -------------------------------------------


@pytest.mark.unit
def test_an_escalated_field_is_never_confirmed_even_by_values() -> None:
    """A heal with no winner arrives as rung 5 (Task 20). There is no node to re-derive, and
    values agreeing with yesterday must not make an unresolved field look confirmed."""
    v = IndependentRungAgreement([ax(AMOUNT_CELL), heuristic(AMOUNT_CELL)]).check(
        UNCHANGED, previous_history(), healed(5)
    )
    assert v.outcome is Outcome.INSUFFICIENT and v.confirmed_by is None
    assert "rung 5" in v.reason


@pytest.mark.unit
def test_a_healer_missing_from_the_injected_rungs_is_insufficient_not_a_value_pass() -> None:
    """Fix round 1 (M2): the pipeline always injects rungs 2-4, so a missing healer is a
    wiring fault -- values agreeing must not paper over it."""
    v = IndependentRungAgreement([ax(AMOUNT_CELL)]).check(UNCHANGED, previous_history(), healed(4))
    assert v.outcome is Outcome.INSUFFICIENT and v.confirmed_by is None
    assert "no DOM evidence: the healing rung was not injected" in v.reason


@pytest.mark.unit
def test_dom_absence_with_values_refusing_is_a_failure() -> None:
    v = IndependentRungAgreement([ax(None), anchors(None), heuristic(AMOUNT_CELL)]).check(
        DECOY, previous_history(), healed(4)
    )
    assert v.outcome is Outcome.FAIL and v.confirmed_by is None
    assert "VALUE family refuses" in v.reason


@pytest.mark.unit
def test_dom_absence_with_no_history_is_insufficient() -> None:
    v = IndependentRungAgreement([ax(None), anchors(None), heuristic(AMOUNT_CELL)]).check(
        UNCHANGED, None, healed(4)
    )
    assert v.outcome is Outcome.INSUFFICIENT and "no basis" in v.reason


@pytest.mark.unit
def test_a_none_family_rung_is_never_a_confirmer() -> None:
    other = FakeRung(5, Family.NONE, PRIOR_AMOUNT_CELL)
    v = IndependentRungAgreement([other, heuristic(AMOUNT_CELL)]).check(UNCHANGED, None, healed(4))
    assert v.outcome is Outcome.INSUFFICIENT and other.calls == 0


class _Boom:
    def __init__(self, rung_id: int, family: Family) -> None:
        self.rung_id, self.family = rung_id, family

    def resolve(self, page: PageSource, target: FieldTarget) -> ResolvedElement | None:
        raise RuntimeError("hostile page")


@pytest.mark.unit
@pytest.mark.parametrize("raiser", ["healer", "confirmer"])
def test_a_rung_that_raises_is_a_failure_not_a_crash(raiser: str) -> None:
    rungs = (
        [ax(AMOUNT_CELL), _Boom(4, Family.STRUCTURAL)]
        if raiser == "healer"
        else [_Boom(2, Family.TEXT_SEMANTIC), heuristic(AMOUNT_CELL)]
    )
    v = IndependentRungAgreement(rungs).check(UNCHANGED, previous_history(), healed(4))
    assert v.outcome is Outcome.FAIL and "RuntimeError" in v.reason
    assert "hostile page" not in v.reason  # never echo what a page made a rung say


@pytest.mark.unit
def test_keys_and_values_are_compared_whitespace_normalised() -> None:
    padded = candidate(
        [
            {"claim_number": f"  {r['claim_number']} ", "amount": f"\n{r['amount']}\t"}
            for r in previous_history().runs[0].rows
        ]
    )
    v = RowKeyContinuity().check(padded, previous_history(), healed(4))
    assert v.outcome is Outcome.PASS and v.observed == "1.0000"


@pytest.mark.unit
def test_rows_lacking_the_key_or_field_are_skipped_not_assumed() -> None:
    rows = [{"amount": "TOTAL"}] + [dict(r) for r in previous_history().runs[0].rows]
    rows[1] = {"claim_number": rows[1]["claim_number"]}  # a row without the healed field
    rate, compared = RowKeyContinuity().agreement(
        candidate(rows), previous_history().runs[0], "amount"
    )
    assert (rate, compared) == (1.0, 19)


@pytest.mark.unit
def test_a_composite_natural_key_joins_on_every_part() -> None:
    prev = AcceptedRun(
        run_key="p",
        window=previous_history().runs[0].window,
        rows=({"a": "1", "b": "x", "v": "10"}, {"a": "1", "b": "y", "v": "20"}),
        natural_key=("a", "b"),
    )
    cand = candidate([{"a": "1", "b": "y", "v": "20"}, {"a": "1", "b": "x", "v": "10"}])
    target = ctx(AMOUNT).target.model_copy(update={"name": "v"})
    v = RowKeyContinuity(min_joined=2).check(cand, History(runs=(prev,)), ctx(target, rung=4))
    assert v.outcome is Outcome.PASS


@pytest.mark.unit
@pytest.mark.parametrize(("threshold", "floor"), [(0.0, 0.5), (1.5, 0.5), (0.9, 0.9), (0.9, 0.0)])
def test_nonsense_thresholds_are_refused(threshold: float, floor: float) -> None:
    with pytest.raises(ValueError):
        RowKeyContinuity(threshold=threshold, collapse_floor=floor)


@pytest.mark.unit
def test_continuity_alert_reads_only_a_steady_state_continuity_dip() -> None:
    dip = RowKeyContinuity().check(CHURNED, previous_history(), ctx(AMOUNT, healed=False, rung=1))
    assert continuity_alert(dip)
    assert not continuity_alert(dip.model_copy(update={"check": "SomethingElse"}))
    assert not continuity_alert(dip.model_copy(update={"observed": None}))
    assert not continuity_alert(
        Verdict(
            check="RowKeyContinuity",
            outcome=Outcome.INSUFFICIENT,
            threshold=None,
            observed="0.1",
            reason="x",
        )
    )
    assert not continuity_alert(
        Verdict(
            check="RowKeyContinuity",
            outcome=Outcome.INSUFFICIENT,
            threshold="garbage",
            observed="0.1",
            reason="x",
        )
    )


@pytest.mark.unit
def test_reasons_never_echo_cell_text() -> None:
    secret = "SECRET-CELL-TEXT"
    rows = [dict(r) for r in previous_history().runs[0].rows]
    for r in rows:
        r["amount"] = secret
    v = RowKeyContinuity().check(candidate(rows), previous_history(), healed(4))
    assert v.outcome is Outcome.FAIL and secret not in v.reason


@pytest.mark.unit
@pytest.mark.parametrize(("run", "written"), [(UNCHANGED, True), (DECOY, False)])
def test_through_the_gate_a_reword_heal_is_written_and_a_decoy_heal_quarantined(
    run: CandidateExtraction, written: bool
) -> None:
    """Both checks on the healed field, RowKeyContinuity on the cache hits: the reword's heal
    is written (C3 fixed), the decoy's quarantined."""
    steady = {t.name: ctx(t, healed=False, rung=1) for t in (CLAIM_NUMBER, RESERVE)}
    contexts = steady | {"amount": healed(4)}
    checks: dict[str, list[Assertion]] = {name: [RowKeyContinuity()] for name in steady}
    checks["amount"] = [
        IndependentRungAgreement([ax(None), anchors(None), heuristic(AMOUNT_CELL)]),
        RowKeyContinuity(),
    ]
    out = gate_run(run, previous_history(), contexts, checks)
    assert isinstance(out, ValidatedExtraction) is written
    assert isinstance(out, QuarantinedRun) is not written


@pytest.mark.unit
def test_a_healer_outside_the_dom_families_has_no_cross_family_counterpart() -> None:
    """A mislabelled NONE-family healer would otherwise let a TEXT rung 'confirm' it."""
    odd_healer = FakeRung(4, Family.NONE, AMOUNT_CELL)
    v = IndependentRungAgreement([ax(AMOUNT_CELL), odd_healer]).check(UNCHANGED, None, healed(4))
    assert v.outcome is Outcome.INSUFFICIENT and "outside the DOM families" in v.reason


# ---- fix round 1: a minimum joined count (Important), duplicate rung ids (M4) ---------------


def _prev(n: int) -> History:
    run = previous_history().runs[0]
    return History(
        runs=(
            AcceptedRun(
                run_key=run.run_key,
                window=run.window,
                rows=run.rows[:n],
                natural_key=run.natural_key,
            ),
        )
    )


@pytest.mark.unit
def test_min_joined_is_published_and_validated() -> None:
    assert MIN_JOINED == 10
    with pytest.raises(ValueError):
        RowKeyContinuity(min_joined=0)


@pytest.mark.unit
def test_one_coincidental_joined_cell_never_confirms_a_heal() -> None:
    v = RowKeyContinuity().check(UNCHANGED, _prev(1), healed(4))
    assert v.outcome is Outcome.INSUFFICIENT and v.confirmed_by is None
    assert "1 joined" in v.reason and "10" in v.reason


@pytest.mark.unit
@pytest.mark.parametrize("n", [1, 2])
def test_a_tiny_join_with_a_change_on_a_cache_hit_is_not_a_collapse(n: int) -> None:
    changed = candidate(
        [
            {"claim_number": r["claim_number"], "amount": "0.01"}
            for r in previous_history().runs[0].rows[:n]
        ]
    )
    v = RowKeyContinuity().check(changed, _prev(n), ctx(AMOUNT, healed=False, rung=1))
    assert v.outcome is Outcome.INSUFFICIENT and not continuity_alert(v)
    assert f"{n} joined" in v.reason


@pytest.mark.unit
def test_exactly_min_joined_is_judged_normally() -> None:
    assert RowKeyContinuity().check(UNCHANGED, _prev(10), healed(4)).outcome is Outcome.PASS
    assert RowKeyContinuity().check(DECOY, _prev(10), healed(4)).outcome is Outcome.FAIL
    assert (
        RowKeyContinuity().check(SWAPPED, _prev(10), ctx(AMOUNT, healed=False, rung=1)).outcome
        is Outcome.FAIL
    )
    assert RowKeyContinuity().check(UNCHANGED, _prev(9), healed(4)).outcome is Outcome.INSUFFICIENT


@pytest.mark.unit
def test_the_value_fallback_with_too_few_joined_is_not_a_pass() -> None:
    v = IndependentRungAgreement([ax(None), anchors(None), heuristic(AMOUNT_CELL)]).check(
        UNCHANGED, _prev(1), healed(4)
    )
    assert v.outcome is Outcome.INSUFFICIENT and v.confirmed_by is None


@pytest.mark.unit
def test_duplicate_rung_ids_are_refused() -> None:
    with pytest.raises(ValueError, match="rung 4"):
        IndependentRungAgreement([heuristic(AMOUNT_CELL), heuristic(PRIOR_AMOUNT_CELL)])
