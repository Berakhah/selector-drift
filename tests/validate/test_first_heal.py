import datetime as dt

import pytest
from bok_core.provenance.claim import Claim, Method
from bok_core.provenance.source import HumanSource

from selector_drift.extract.models import CandidateExtraction
from selector_drift.resolve.cache import CachedSelector
from selector_drift.resolve.trace import AnchorEvidence
from selector_drift.scope.record import FirstHealPolicy
from selector_drift.validate.assertions.first_heal import FirstHealConfirmation
from selector_drift.validate.gate import ValidatedExtraction, gate
from selector_drift.validate.quarantine import QuarantinedRun
from selector_drift.validate.verdict import Outcome
from tests.validate.helpers import AT, DictCache, claim, column, ctx

REQUIRED, AUTO = FirstHealPolicy.REQUIRED, FirstHealPolicy.AUTO_PROMOTE
NEW = "td[data-testid=claim]"
HEALED_TO = column("claim_number", ["AB12345678"], selector=NEW)
RUN_KEY = "testbench:2:2026-10-01"


def cache_holding(selector: str) -> DictCache:
    anchor = AnchorEvidence(accessible_name=None, label_text=None, header_text="Claim Number")
    return DictCache(
        {"claim_number": CachedSelector(selector=selector, anchor=anchor, set_by_rung=4)}
    )


def rows_with(selectors: list[str | None]) -> CandidateExtraction:
    """One row per selector; None is a cell whose source is not a DOM element."""
    rows = []
    for i, sel in enumerate(selectors):
        value = f"AB{i:08d}"
        if sel is None:
            c = Claim(
                value=value,
                method=Method.DETERMINISTIC,
                confidence=None,
                observed_at=AT,
                source=HumanSource(who="t", at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)),
            )
        else:
            c = claim(value, selector=sel)
        rows.append({"claim_number": c})
    return CandidateExtraction(rows=rows, trace={}, spec_version=2, run_key=RUN_KEY)


@pytest.mark.unit
def test_first_heal_to_a_new_selector_requires_a_human_by_default():
    v = FirstHealConfirmation(REQUIRED).check(
        HEALED_TO, None, ctx(rung=4, cache=cache_holding("#old"))
    )
    assert v.outcome is Outcome.INSUFFICIENT and v.observed is not None
    assert NEW in v.observed


@pytest.mark.unit
def test_repeat_heal_to_a_known_selector_runs_automatically():
    v = FirstHealConfirmation(REQUIRED).check(
        HEALED_TO, None, ctx(rung=4, cache=cache_holding(NEW))
    )
    assert v.outcome is Outcome.PASS


@pytest.mark.unit
def test_auto_promote_is_permitted_only_where_the_scope_record_says_so():
    """Design 5.8. The bench sets auto_promote because it is synthetic with a known answer
    key. The policy travels from the scope record, so it cannot be quietly set for a client."""
    v = FirstHealConfirmation(AUTO).check(HEALED_TO, None, ctx(rung=4))
    assert v.outcome is Outcome.PASS and "auto_promote" in v.reason


@pytest.mark.unit
def test_human_confirmation_satisfies_the_check():
    v = FirstHealConfirmation(REQUIRED).check(HEALED_TO, None, ctx(rung=4, confirmed=True))
    assert v.outcome is Outcome.PASS


@pytest.mark.unit
def test_nothing_healed_needs_no_confirmation():
    v = FirstHealConfirmation(REQUIRED).check(HEALED_TO, None, ctx(healed=False, rung=1))
    assert v.outcome is Outcome.PASS


@pytest.mark.unit
def test_the_default_policy_fails_closed():
    assert FirstHealConfirmation().policy is REQUIRED


@pytest.mark.unit
def test_a_first_heal_completes_into_quarantine_and_confirmation_releases_it():
    # Brief: "the first heal to a new selector completes into quarantine; a human confirms".
    assert isinstance(gate(HEALED_TO, None, ctx(rung=4), [FirstHealConfirmation()]), QuarantinedRun)
    assert isinstance(
        gate(HEALED_TO, None, ctx(rung=4, confirmed=True), [FirstHealConfirmation()]),
        ValidatedExtraction,
    )


@pytest.mark.unit
def test_an_unconfirmed_first_heal_never_passes_under_the_default_policy():
    for cache in (None, cache_holding("#old")):
        v = FirstHealConfirmation().check(HEALED_TO, None, ctx(rung=4, cache=cache))
        assert v.outcome is not Outcome.PASS


@pytest.mark.unit
def test_every_cell_on_the_cached_selector_passes():
    cand = rows_with([NEW, NEW, NEW])
    v = FirstHealConfirmation().check(cand, None, ctx(rung=4, cache=cache_holding(NEW)))
    assert v.outcome is Outcome.PASS


@pytest.mark.unit
def test_one_cell_matching_the_cache_does_not_vouch_for_rows_from_another_selector():
    cand = rows_with([NEW, "td.other", NEW])
    v = FirstHealConfirmation().check(cand, None, ctx(rung=4, cache=cache_holding(NEW)))
    assert v.outcome is Outcome.INSUFFICIENT
    assert "2 distinct selectors" in v.reason
    assert "AB0000" not in v.reason and "AB0000" not in (v.observed or "")


@pytest.mark.unit
def test_a_cell_without_a_selector_among_cached_selector_cells_is_insufficient():
    cand = rows_with([NEW, None, NEW])
    v = FirstHealConfirmation().check(cand, None, ctx(rung=4, cache=cache_holding(NEW)))
    assert v.outcome is Outcome.INSUFFICIENT


@pytest.mark.unit
def test_a_healed_field_with_zero_cells_is_insufficient_not_pass():
    cand = CandidateExtraction(rows=[], trace={}, spec_version=2, run_key=RUN_KEY)
    v = FirstHealConfirmation().check(cand, None, ctx(rung=4, cache=cache_holding(NEW)))
    assert v.outcome is Outcome.INSUFFICIENT
