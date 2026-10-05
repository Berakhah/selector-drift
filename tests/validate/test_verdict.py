import pytest
from pydantic import TypeAdapter, ValidationError

from selector_drift.resolve.trace import Family
from selector_drift.validate.verdict import Confirmation, Outcome, Verdict


@pytest.mark.unit
def test_a_verdict_must_give_a_reason():
    # Design 5.5: no bare red badges. An empty reason is a bare badge with extra steps.
    with pytest.raises(ValidationError):
        Verdict(check="X", outcome=Outcome.FAIL, threshold=None, observed=None, reason="")


@pytest.mark.unit
def test_only_a_pass_can_name_a_confirmer():
    # heal_events.confirmer_* is written from confirmed_by (Task 26A). A FAIL naming a
    # confirmer would publish a confirmation that never happened.
    with pytest.raises(ValidationError, match="only a PASS"):
        Verdict(
            check="X",
            outcome=Outcome.FAIL,
            threshold=None,
            observed=None,
            reason="r",
            confirmed_by=Confirmation(rung=4, family=Family.STRUCTURAL),
        )


@pytest.mark.unit
def test_verdicts_round_trip_through_json_as_heal_events_store_them():
    v = Verdict(
        check="RowKeyContinuity",
        outcome=Outcome.PASS,
        threshold=">= 0.95",
        observed="1.0000",
        reason="agrees",
        field="amount",
        confirmed_by=Confirmation(rung=None, family=Family.VALUE),
    )
    dumped = TypeAdapter(list[Verdict]).dump_python([v], mode="json")
    assert dumped[0]["outcome"] == "PASS" and dumped[0]["confirmed_by"]["family"] == "VALUE"
    assert TypeAdapter(list[Verdict]).validate_python(dumped) == [v]
