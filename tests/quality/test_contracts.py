import datetime as dt
import json

import pytest
from bok_core.provenance.claim import Method
from bok_core.provenance.source import DomSource

from selector_drift._bok_shim import Finding, Severity, to_sarif
from selector_drift.db.models import AuditLog, FindingRow
from selector_drift.db.session import Session
from selector_drift.quality.contracts import (
    PERSISTENCE,
    PSI_REASON,
    PSI_THRESHOLD,
    REFERENCE_WINDOWS,
    evaluate,
)
from selector_drift.validate.assertions.distribution import (
    ALPHA,
    MIN_VALUES,
    REFERENCE_RUNS,
    DistributionalAssertion,
)
from tests.land.factory import SNAPSHOT, rows
from tests.quality.conftest import BASE

DAY = BASE + dt.timedelta(days=6)
# Every rule at once. The brief's rows(40, blank_amounts=20) leaves 20 non-blank amounts, under
# Task 23's MIN_VALUES (40): no basis, so no shift finding. 49 rows (< half the median of 100),
# 5 blank (10% > the 5% floor) leaves 44 non-blank amounts -- enough evidence to judge.
ALL_FOUR = {"scale": 3.0, "blank_amounts": 5, "extra": {"surprise": "x"}}


def _history(land, n: int) -> None:
    for i in range(n):
        land(BASE + dt.timedelta(days=i), rows())


def _rules(findings: list[Finding]) -> set[str]:
    return {f.properties["rule"] for f in findings}


# ------------------------------------------------------------------ constants


@pytest.mark.unit
def test_the_quality_rule_and_the_gate_share_their_parameters() -> None:
    da = DistributionalAssertion()
    assert (da.threshold, da.persistence) == (PSI_THRESHOLD, PERSISTENCE)
    assert REFERENCE_WINDOWS == REFERENCE_RUNS


@pytest.mark.unit
def test_the_shift_reason_cites_the_statistic_actually_used() -> None:
    assert "Jeffreys" in PSI_REASON and str(MIN_VALUES) in PSI_REASON
    assert f"{ALPHA:g}" in PSI_REASON
    assert "epsilon" not in PSI_REASON.lower()


# ------------------------------------------------------------------ the brief's tests


@pytest.mark.integration
def test_a_clean_run_produces_no_findings(land):
    _history(land, 6)
    assert evaluate(land(DAY, rows())) == []


@pytest.mark.integration
def test_row_count_shortfall_is_reported(land):
    _history(land, 6)
    f = next(f for f in evaluate(land(DAY, rows(40))) if f.properties["rule"] == "row_count")
    assert f.severity is Severity.HIGH and f.properties["observed"] == "40"


@pytest.mark.integration
def test_row_count_waits_for_a_basis(land):
    _history(land, 2)  # fewer than three windows: no judgement yet
    assert "row_count" not in _rules(evaluate(land(DAY, rows(10))))


@pytest.mark.integration
def test_row_count_baseline_is_the_median_so_one_spike_does_not_move_the_bar(land):
    for i in range(5):
        land(BASE + dt.timedelta(days=i), rows(1000 if i == 4 else 100))  # one month-end spike
    assert "row_count" not in _rules(evaluate(land(DAY, rows(60))))  # mean 280 would flag 60


@pytest.mark.integration
def test_null_rate_spike_is_reported(land):
    _history(land, 6)
    f = next(
        f
        for f in evaluate(land(DAY, rows(blank_amounts=50)))
        if f.properties["rule"] == "null_rate"
    )
    assert f.properties["field"] == "amount" and f.properties["observed"] == "0.500"


@pytest.mark.integration
def test_schema_drift_is_reported_against_the_specs_expected_schema(land):
    # M4. The first draft tested schema drift against no baseline.
    _history(land, 6)
    f = next(
        f
        for f in evaluate(land(DAY, rows(extra={"surprise": "x"})))
        if f.properties["rule"] == "schema_drift"
    )
    assert f.properties["unexpected"] == ["surprise"] and f.properties["missing"] == []


@pytest.mark.integration
def test_a_missing_column_is_schema_drift(land):
    _history(land, 6)
    only_keys = [{"claim_number": r["claim_number"]} for r in rows()]
    fs = evaluate(land(DAY, only_keys))
    f = next(f for f in fs if f.properties["rule"] == "schema_drift")
    assert f.properties["missing"] == ["amount"]
    # One fact, one finding: a column that is absent is schema drift, not also a 100% null rate.
    assert "null_rate" not in _rules(fs)


@pytest.mark.integration
def test_a_mistyped_column_is_schema_drift_and_names_no_value(land):
    _history(land, 6)
    f = next(
        f
        for f in evaluate(land(DAY, rows(extra={"amount": "n/a"})))
        if f.properties["rule"] == "schema_drift"
    )
    assert f.properties["mistyped"] == {"amount": 100}
    assert "n/a" not in str(f.properties)  # counts, never values


@pytest.mark.integration
def test_distribution_shift_is_detected_after_warm_up(land):
    _history(land, 6)  # the fixture scope warms up in 5
    f = next(
        f
        for f in evaluate(land(DAY, rows(scale=3.0)))
        if f.properties["rule"] == "distribution_shift"
    )
    assert f.properties["field"] == "amount"
    assert float(f.properties["observed"]) > PSI_THRESHOLD
    assert float(f.properties["observed"]) > float(f.properties["critical"])
    assert f.severity is Severity.MEDIUM  # one run is month-end, not an alert


@pytest.mark.integration
def test_no_distribution_judgement_during_warm_up(land):
    _history(land, 3)
    assert "distribution_shift" not in _rules(evaluate(land(DAY, rows(scale=3.0))))


@pytest.mark.integration
def test_no_distribution_judgement_on_too_few_values(land):
    # Task 23's MIN_VALUES: 30 amounts are too few to be evidence -- no basis, no finding.
    _history(land, 6)
    assert "distribution_shift" not in _rules(evaluate(land(DAY, rows(30, scale=3.0))))


@pytest.mark.integration
def test_a_shift_that_persists_escalates_to_high(land):
    # B7: persistence before paging. Two consecutive runs breach -> HIGH -> Task 29A pages.
    _history(land, 6)
    evaluate(land(DAY, rows(scale=3.0)))
    f = next(
        f
        for f in evaluate(land(DAY + dt.timedelta(days=1), rows(scale=3.0)))
        if f.properties["rule"] == "distribution_shift"
    )
    assert f.severity is Severity.HIGH
    assert f.properties["consecutive"] == 2


@pytest.mark.integration
def test_a_shift_the_client_marked_expected_does_not_escalate(land):
    _history(land, 6)
    first = land(DAY, rows(scale=3.0))
    evaluate(first)
    with Session() as s:
        s.add(
            AuditLog(
                at=dt.datetime.now(dt.UTC),
                actor="op1",
                action="marked_expected",
                subject=first,
                detail={"field": "amount"},
            )
        )
        s.commit()
    f = next(
        f
        for f in evaluate(land(DAY + dt.timedelta(days=1), rows(scale=3.0)))
        if f.properties["rule"] == "distribution_shift"
    )
    assert f.severity is Severity.MEDIUM


@pytest.mark.integration
def test_every_finding_states_its_threshold_and_why(land):
    _history(land, 6)
    fs = evaluate(land(DAY, rows(49, **ALL_FOUR)))
    assert _rules(fs) == {"row_count", "null_rate", "schema_drift", "distribution_shift"}
    for f in fs:
        assert f.properties["threshold"] and f.properties["threshold_reason"]
        assert "observed" in f.properties and "field" in f.properties


@pytest.mark.integration
def test_the_shift_finding_carries_the_gates_numbers(land):
    _history(land, 6)
    f = next(
        f
        for f in evaluate(land(DAY, rows(scale=3.0)))
        if f.properties["rule"] == "distribution_shift"
    )
    p = f.properties
    assert (p["alpha"], p["reference_n"], p["current_n"]) == (ALPHA, 600, 100)
    assert float(p["critical"]) > 0 and p["threshold"] == f"PSI {PSI_THRESHOLD:.2f}"


@pytest.mark.integration
def test_every_finding_carries_the_claim_chain_it_was_derived_from(land):
    # INTERFACE-CONTRACT 4: `claim` is required. I1: a DERIVED claim's confidence may not
    # exceed its parents'; I3: DETERMINISTIC parents have none -- so neither does the finding.
    _history(land, 6)
    fs = evaluate(land(DAY, rows(49, **ALL_FOUR)))
    assert fs
    for f in fs:
        assert f.claim.method is Method.DERIVED and f.claim.parents
        assert f.claim.confidence is None
        for parent in f.claim.parents:
            assert parent.method is Method.DETERMINISTIC and parent.confidence is None
            assert isinstance(parent.source, DomSource)
            assert parent.source.snapshot_id == SNAPSHOT


@pytest.mark.integration
def test_no_extracted_value_appears_in_any_property_or_title(land):
    # Brief rule 6: values live only in the claim chain.
    _history(land, 6)
    plain = rows(49, **ALL_FOUR)
    fs = evaluate(land(DAY, plain))
    cells = {v for r in plain for v in r.values() if len(v) > 1}
    for f in fs:
        outside_chain = json.dumps(f.properties) + f.title
        assert not [v for v in cells if v in outside_chain], f.properties["rule"]


@pytest.mark.integration
def test_findings_are_persisted_with_the_pinned_row_shape(land):
    _history(land, 6)
    key = land(DAY, rows(40))
    fs = evaluate(key)
    with Session() as s:
        stored = {r.id: r for r in s.query(FindingRow).filter_by(run_key=key)}
    assert set(stored) == {f"{f.id}@{key}" for f in fs}
    for f in fs:
        row = stored[f"{f.id}@{key}"]
        assert row.severity == f.severity.value and row.rule == f.properties["rule"]
        assert Finding.model_validate(row.payload) == f


@pytest.mark.integration
def test_re_evaluating_a_replayed_run_replaces_its_findings(land):
    _history(land, 6)
    key = land(DAY, rows(40))
    first = evaluate(key)
    second = evaluate(key)
    with Session() as s:
        assert s.query(FindingRow).filter_by(run_key=key).count() == len(first)
    assert [(f.id, f.severity) for f in second] == [(f.id, f.severity) for f in first]


@pytest.mark.integration
def test_re_evaluating_a_breach_does_not_count_itself_as_its_own_persistence(land):
    _history(land, 6)
    key = land(DAY, rows(scale=3.0))
    evaluate(key)
    f = next(f for f in evaluate(key) if f.properties["rule"] == "distribution_shift")
    assert f.severity is Severity.MEDIUM


@pytest.mark.integration
def test_findings_export_through_the_shared_sarif_emitter(land):
    # Design 9.4: the shared ladder and the shared emitter, with the chain on every result.
    _history(land, 6)
    fs = evaluate(land(DAY, rows(40)))
    sarif = to_sarif(fs, tool_name="selector-drift", tool_version="0.1.0")
    results = sarif["runs"][0]["results"]
    assert [r["properties"]["bok_severity"] for r in results] == [f.severity.value for f in fs]
    assert all("bok_provenance" in r["properties"] for r in results)


@pytest.mark.integration
def test_the_quality_psi_is_the_gates_psi(land):
    """Ledger 2026-09-26: one statistic. The finding's observed PSI is psi_of_values over the
    same reference and current values the gate's DistributionalAssertion would read."""
    from selector_drift.validate.stats import psi_of_values

    _history(land, 6)
    key = land(DAY, rows(scale=3.0))
    f = next(f for f in evaluate(key) if f.properties["rule"] == "distribution_shift")
    ref = [r["amount"] for i in range(6) for r in rows()]
    cur = [r["amount"] for r in rows(scale=3.0)]
    assert float(f.properties["observed"]) == pytest.approx(psi_of_values(ref, cur)[0], abs=1e-4)


# ------------------------------------------------------------------ first run, unknown run


@pytest.mark.integration
def test_the_first_run_of_a_target_is_warm_up_not_a_crash(land):
    # Task 28 pointer: with no prior files, the lake has no data columns to bind -- no history
    # means warm-up and nothing to compare, never a binder error.
    assert evaluate(land(DAY, rows())) == []


@pytest.mark.integration
def test_a_run_that_never_landed_is_refused(land):
    _history(land, 1)
    with pytest.raises(LookupError, match="no landed files"):
        evaluate(f"testbench:2:{DAY.isoformat()}")
