import pytest

from selector_drift.validate.gate import (
    _TOKEN,
    HealContext,
    ValidatedExtraction,
    _effective,
    gate,
    gate_run,
)
from selector_drift.validate.quarantine import QuarantinedRun
from selector_drift.validate.verdict import Outcome, Verdict
from tests.validate.helpers import (
    AMOUNT,
    CLAIM_NUMBER,
    PAGE,
    AlwaysFail,
    AlwaysInsufficient,
    AlwaysPass,
    EmptyCache,
    Raises,
    candidate,
    ctx,
)

CANDIDATE = candidate([{"claim_number": "AB12345678", "amount": "1,200.00"}])


@pytest.mark.unit
def test_insufficient_is_tolerated_on_a_cache_hit():
    c = ctx(healed=False, rung=1)
    assert isinstance(gate(CANDIDATE, None, c, [AlwaysInsufficient()]), ValidatedExtraction)


@pytest.mark.unit
def test_insufficient_is_a_failure_on_a_heal():
    """Design 5.7. When the selector changed we demand full evidence; when nothing
    changed we do not. This is what stops the distributional check crying wolf."""
    assert isinstance(gate(CANDIDATE, None, ctx(rung=3), [AlwaysInsufficient()]), QuarantinedRun)


@pytest.mark.unit
@pytest.mark.parametrize(("healed", "rung"), [(False, 1), (True, 3)])
def test_any_fail_quarantines_regardless_of_heal(healed, rung):
    assert isinstance(
        gate(CANDIDATE, None, ctx(healed=healed, rung=rung), [AlwaysFail()]), QuarantinedRun
    )


@pytest.mark.unit
def test_validated_extraction_cannot_be_built_outside_validate():
    with pytest.raises(TypeError):
        ValidatedExtraction(rows=(), run_key="x", verdicts=())  # type: ignore[call-arg]


@pytest.mark.unit
def test_a_forged_token_is_rejected_by_identity_not_by_type():
    # M29's lesson from the scope gate: an isinstance check is satisfied by a second instance.
    forged = type(_TOKEN)()
    with pytest.raises(TypeError, match=r"minted only by validate\.gate"):
        ValidatedExtraction(rows=(), run_key="x", verdicts=(), _token=forged)


@pytest.mark.unit
def test_a_quarantined_run_carries_every_verdict_stamped_with_its_field():
    q = gate(CANDIDATE, None, ctx(rung=3), [AlwaysFail(), AlwaysPass()])
    assert isinstance(q, QuarantinedRun)
    assert len(q.verdicts) == 2 and {v.field for v in q.verdicts} == {"claim_number"}
    assert q.reason == "gate failed: claim_number:AlwaysFail" and q.data_uri == ""


@pytest.mark.unit
def test_one_failing_field_quarantines_the_whole_run():
    ok = ctx(CLAIM_NUMBER, healed=False, rung=1)
    bad = ctx(AMOUNT, healed=True, rung=3)
    r = gate_run(
        CANDIDATE,
        None,
        {"claim_number": ok, "amount": bad},
        {"claim_number": [AlwaysPass()], "amount": [AlwaysInsufficient()]},
    )
    assert isinstance(r, QuarantinedRun)
    assert {v.field for v in r.verdicts} == {"claim_number", "amount"}


@pytest.mark.unit
@pytest.mark.parametrize("rung", [5, 0])
def test_an_escalated_field_never_writes(rung):
    # Design 5.3/5.7: rung 5 escalates, it does not heal. Task 26A passes `trace.winner or 5`.
    r = gate(CANDIDATE, None, ctx(rung=rung, confirmed=True), [AlwaysPass()])
    assert isinstance(r, QuarantinedRun)
    assert any(v.check == "Escalation" and v.outcome is Outcome.FAIL for v in r.verdicts)


@pytest.mark.unit
def test_a_check_that_raises_is_a_failure_not_a_crash():
    # A crash would lose the run's data; a FAIL keeps it in quarantine with the reason.
    r = gate(CANDIDATE, None, ctx(healed=False, rung=1), [Raises()])
    assert isinstance(r, QuarantinedRun)
    (v,) = r.verdicts
    assert v.check == "Raises" and v.outcome is Outcome.FAIL and v.observed == "RuntimeError"


@pytest.mark.unit
def test_a_field_with_no_checks_is_refused_not_waved_through():
    with pytest.raises(ValueError, match="judged by nothing"):
        gate_run(CANDIDATE, None, {"claim_number": ctx()}, {"claim_number": []})
    with pytest.raises(ValueError, match="nothing to judge"):
        gate_run(CANDIDATE, None, {}, {})


@pytest.mark.unit
def test_a_validated_extraction_carries_the_verdicts_it_was_judged_by():
    # Task 26B's confirmer_of() and Task 26A's record_heals read these.
    v = gate(CANDIDATE, None, ctx(rung=3), [AlwaysPass("A"), AlwaysPass("B")])
    assert isinstance(v, ValidatedExtraction)
    assert [x.check for x in v.verdicts] == ["A", "B"] and v.run_key == CANDIDATE.run_key
    assert v.rows == tuple(CANDIDATE.rows)


@pytest.mark.unit
def test_human_confirmation_supplies_missing_evidence_but_never_overrides_a_failure():
    # Design 5.9: promotion supplies the missing evidence; it does not skip a check.
    confirmed = ctx(rung=3, confirmed=True)
    assert isinstance(gate(CANDIDATE, None, confirmed, [AlwaysInsufficient()]), ValidatedExtraction)
    assert isinstance(gate(CANDIDATE, None, confirmed, [AlwaysFail()]), QuarantinedRun)


@pytest.mark.unit
def test_gate_is_the_one_field_case_of_gate_run():
    c = ctx(rung=3)
    one = gate(CANDIDATE, None, c, [AlwaysFail()])
    run = gate_run(CANDIDATE, None, {"claim_number": c}, {"claim_number": [AlwaysFail()]})
    assert one == run


# -- ledger pointers (Task 20 dispatch) ---------------------------------------------------


@pytest.mark.unit
def test_a_cache_hit_runs_every_check_and_a_fail_on_it_still_quarantines():
    # Pointer 2: ValueSwap arrives as a rung-1 CACHE HIT (right node, swapped value). Rung 1
    # is only the lenient path for INSUFFICIENT -- every check still runs, and a FAIL from
    # one (Task 22's RowKeyContinuity) still quarantines the whole run.
    passing, failing, insufficient = AlwaysPass(), AlwaysFail("RowKeyStub"), AlwaysInsufficient()
    r = gate_run(
        CANDIDATE,
        None,
        {
            "claim_number": ctx(CLAIM_NUMBER, healed=False, rung=1),
            "amount": ctx(AMOUNT, healed=False, rung=1),
        },
        {"claim_number": [passing, insufficient], "amount": [failing]},
    )
    assert isinstance(r, QuarantinedRun)
    assert (passing.calls, failing.calls, insufficient.calls) == (1, 1, 1)
    assert r.reason == "gate failed: amount:RowKeyStub"
    assert [(v.field, v.check) for v in r.verdicts] == [
        ("claim_number", "AlwaysPass"),
        ("claim_number", "AlwaysInsufficient"),
        ("amount", "RowKeyStub"),
    ]


@pytest.mark.unit
def test_an_escalated_field_is_still_judged_and_names_why_it_never_writes():
    # Pointer 1: CascadeTrace.healed is True when winner is None. The pipeline passes
    # `trace.winner or 5`; the field's checks still run (evidence for the human) and an
    # Escalation FAIL is added that no confirmation can relax.
    check = AlwaysPass()
    r = gate(CANDIDATE, None, ctx(rung=5, confirmed=True), [check])
    assert isinstance(r, QuarantinedRun) and check.calls == 1
    (esc,) = [v for v in r.verdicts if v.check == "Escalation"]
    assert esc.field == "claim_number" and esc.observed == "rung 5"
    assert "escalat" in esc.reason and r.reason == "gate failed: claim_number:Escalation"


@pytest.mark.unit
@pytest.mark.parametrize(("healed", "rung"), [(False, 3), (False, 5), (False, 0), (True, 1)])
def test_a_context_whose_heal_flag_contradicts_its_rung_is_refused(healed, rung):
    # Fail closed: `healed` is exactly `winner != 1` (CascadeTrace.healed). A field that won
    # on rung 3 but claims healed=False would ride the lenient INSUFFICIENT path unearned.
    with pytest.raises(ValueError, match="healed"):
        HealContext(
            healed=healed, winning_rung=rung, page=PAGE, target=CLAIM_NUMBER, cache=EmptyCache()
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("outcome", "strict", "expected"),
    [
        (Outcome.PASS, True, Outcome.PASS),
        (Outcome.PASS, False, Outcome.PASS),
        (Outcome.FAIL, True, Outcome.FAIL),
        (Outcome.FAIL, False, Outcome.FAIL),
        (Outcome.INSUFFICIENT, True, Outcome.FAIL),
        (Outcome.INSUFFICIENT, False, Outcome.INSUFFICIENT),
    ],
)
def test_effective_outcome_is_the_whole_policy(outcome, strict, expected):
    v = Verdict(check="X", outcome=outcome, threshold=None, observed=None, reason="r")
    assert _effective(v, strict) is expected
