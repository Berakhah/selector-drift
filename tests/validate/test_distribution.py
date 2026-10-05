from __future__ import annotations

import dataclasses
import datetime as dt
import math

import pytest

from selector_drift.scope.gate import authorise
from selector_drift.scope.record import AuthorisedTarget, Basis, ScopeRecord
from selector_drift.validate.assertions.base import Assertion
from selector_drift.validate.assertions.distribution import (
    MIN_VALUES,
    REFERENCE_RUNS,
    Assessment,
    DistributionalAssertion,
)
from selector_drift.validate.gate import QuarantinedRun, ValidatedExtraction, gate_run
from selector_drift.validate.history import History
from selector_drift.validate.stats import psi_of_values
from selector_drift.validate.verdict import Outcome
from tests.validate.helpers import (
    AMOUNT,
    LOW_BAND,
    SAME,
    SHIFTED,
    STATUS,
    amount_history,
    column,
    ctx,
    grid,
    history_of,
)

HEALED = ctx(AMOUNT, healed=True, rung=3)
STEADY = ctx(AMOUNT, healed=False, rung=1)
DA = DistributionalAssertion()


def _target(**over: object) -> AuthorisedTarget:
    rec = ScopeRecord(
        target="testbench",
        basis=Basis.SELF_OWNED,
        authorising_party="me",
        authorised_on=dt.date(2026, 9, 1),
        expires_on=dt.date(2027, 9, 1),
        evidence_uri=None,
        evidence_sha256=None,
        origins=["https://bench.example.invalid"],
        rate_limit={"requests": 1, "per_seconds": 30},  # type: ignore[arg-type]
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="auto_promote",  # type: ignore[arg-type]
        legal_review="self_drafted",
        **over,  # type: ignore[arg-type]
    )
    return authorise(rec, today=dt.date(2026, 10, 1))


@pytest.mark.unit
def test_it_satisfies_the_assertion_protocol() -> None:
    check: Assertion = DA
    assert check.name == "DistributionalAssertion" and REFERENCE_RUNS == 30


@pytest.mark.unit
def test_warmup_returns_insufficient_not_pass() -> None:
    v = DA.check(SAME, amount_history(5), HEALED)
    assert v.outcome is Outcome.INSUFFICIENT and "warm-up" in v.reason


@pytest.mark.unit
def test_no_history_at_all_is_warmup() -> None:
    v = DA.check(SAME, None, HEALED)
    assert v.outcome is Outcome.INSUFFICIENT and "warm-up: 0 of 30" in v.reason


@pytest.mark.unit
def test_warmup_length_comes_from_the_scope_record_not_a_global() -> None:
    # M18. 30 daily runs is 30 days of quarantining every heal on the bench.
    assert DistributionalAssertion.for_target(_target(warmup_runs=5)).warmup_runs == 5
    assert DistributionalAssertion.for_target(_target()).warmup_runs == 30
    v = DistributionalAssertion(warmup_runs=5).check(SAME, amount_history(5), HEALED)
    assert v.outcome is Outcome.PASS


@pytest.mark.unit
def test_a_single_run_shift_blocks_when_the_selector_healed() -> None:
    # M17. The heal IS the event. Waiting for a second consecutive run means the
    # first day's bad data is already written -- which is what the gate exists to stop.
    v = DA.check(SHIFTED, amount_history(40), HEALED)
    assert v.outcome is Outcome.FAIL and float(v.observed or "nan") > 0.20


@pytest.mark.unit
def test_seasonality_does_not_alert_before_persistence_is_met() -> None:
    # B7. One shifted run is month-end. Two in a row is a problem. Steady state never blocks.
    h = amount_history(40)
    v = DA.check(SHIFTED, h, STEADY)
    assert v.outcome is Outcome.PASS and "no alert yet" in v.reason
    a = DA.assess(SHIFTED, h, "amount")
    assert isinstance(a, Assessment) and a.breach and not a.alert and a.prior_breaches == 0


@pytest.mark.unit
def test_a_persistent_shift_alerts_but_still_does_not_block_a_cache_hit() -> None:
    h = amount_history(40, prior_breaches=1)
    v = DA.check(SHIFTED, h, STEADY)
    assert v.outcome is Outcome.PASS and "alert is raised" in v.reason
    a = DA.assess(SHIFTED, h, "amount")
    assert isinstance(a, Assessment) and a.alert and a.prior_breaches == 1


@pytest.mark.unit
def test_prior_breaches_count_only_the_consecutive_trailing_run() -> None:
    # A breach three runs ago, then a clean run: the streak is broken.
    base = amount_history(40, prior_breaches=3)
    broken = History(
        runs=(*base.runs[:-1], dataclasses.replace(base.runs[-1], shift_alerts=frozenset()))
    )
    a = DA.assess(SHIFTED, broken, "amount")
    assert isinstance(a, Assessment) and a.prior_breaches == 0 and not a.alert


@pytest.mark.unit
def test_a_longer_persistence_needs_a_longer_streak() -> None:
    three = DistributionalAssertion(persistence=3)
    one = three.assess(SHIFTED, amount_history(40, prior_breaches=1), "amount")
    two = three.assess(SHIFTED, amount_history(40, prior_breaches=2), "amount")
    assert isinstance(one, Assessment) and not one.alert
    assert isinstance(two, Assessment) and two.alert


@pytest.mark.unit
def test_client_marked_expected_suppresses_the_alert() -> None:
    a = DA.assess(SHIFTED, amount_history(40, prior_breaches=1, marked_expected=True), "amount")
    assert isinstance(a, Assessment) and a.breach and a.suppressed and not a.alert
    v = DA.check(SHIFTED, amount_history(40, prior_breaches=1, marked_expected=True), STEADY)
    assert v.outcome is Outcome.PASS and "marked this shift expected" in v.reason


@pytest.mark.unit
def test_marking_a_shift_expected_does_not_unblock_a_heal() -> None:
    h = amount_history(40, prior_breaches=1, marked_expected=True)
    assert DA.check(SHIFTED, h, HEALED).outcome is Outcome.FAIL


@pytest.mark.unit
def test_a_bin_with_no_observations_does_not_return_infinity() -> None:
    # M16. PSI is undefined at log(0). Without epsilon this silently returns inf,
    # which compares greater than every threshold and quarantines everything.
    v = DA.check(LOW_BAND, amount_history(40), HEALED)
    assert math.isfinite(float(v.observed or "nan")) and v.outcome is Outcome.FAIL


@pytest.mark.unit
def test_the_same_distribution_scores_near_zero_and_passes_a_heal() -> None:
    v = DA.check(SAME, amount_history(40), HEALED)
    assert v.outcome is Outcome.PASS and float(v.observed or "nan") < 0.05
    assert v.reason.startswith("distribution unchanged: PSI ")
    assert "n=3000 reference, 100 current" in v.reason


@pytest.mark.unit
def test_a_steady_state_run_with_no_breach_passes_without_comment() -> None:
    v = DA.check(SAME, amount_history(40), STEADY)
    assert v.outcome is Outcome.PASS and v.reason.startswith("distribution unchanged")


@pytest.mark.unit
def test_bins_come_from_the_reference_window_only() -> None:
    # Runs older than the trailing 30 do not move the edges...
    base = amount_history(40)
    noisy = History(
        runs=(
            *(
                dataclasses.replace(
                    r, rows=tuple(dict(row) | {"amount": "999,999.00"} for row in r.rows)
                )
                for r in base.runs[:10]
            ),
            *base.runs[10:],
        )
    )
    assert DA.bin_edges_for(base, "amount") == DA.bin_edges_for(noisy, "amount")
    # ...and a run inside the window does. The current run is not an argument at all.
    moved = History(
        runs=(
            *base.runs[:-1],
            dataclasses.replace(
                base.runs[-1],
                rows=tuple(
                    dict(row) | {"amount": v}
                    for row, v in zip(base.runs[-1].rows, grid(5000, 9), strict=True)
                ),
            ),
        )
    )
    assert DA.bin_edges_for(base, "amount") != DA.bin_edges_for(moved, "amount")


@pytest.mark.unit
def test_bin_edges_for_a_text_field_are_empty() -> None:
    h = history_of("status", 40, values=lambda i: ["Open", "Paid"] * 50)
    assert DA.bin_edges_for(h, "status") == ()


@pytest.mark.unit
def test_the_verdict_always_states_the_threshold() -> None:
    assert DA.check(SAME, amount_history(3), HEALED).threshold == "PSI 0.20"
    assert DA.check(SAME, amount_history(40), HEALED).threshold == "PSI 0.20"


@pytest.mark.unit
def test_a_categorical_field_is_compared_by_category_proportions() -> None:
    h = history_of("status", 40, values=lambda i: ["Open", "Paid"] * 50)
    same = DA.check(column("status", ["Open", "Paid"] * 50), h, ctx(STATUS, rung=3))
    moved = DA.check(column("status", ["Closed"] * 100), h, ctx(STATUS, rung=3))
    assert same.outcome is Outcome.PASS and moved.outcome is Outcome.FAIL
    assert "category proportions" in moved.reason


@pytest.mark.unit
def test_unparseable_values_count_as_a_shift_not_as_missing_rows() -> None:
    v = DA.check(column("amount", ["n/a"] * 100), amount_history(40), HEALED)
    assert v.outcome is Outcome.FAIL


@pytest.mark.unit
def test_no_current_values_is_insufficient() -> None:
    v = DA.check(column("amount", []), amount_history(40), HEALED)
    assert v.outcome is Outcome.INSUFFICIENT


@pytest.mark.unit
def test_rows_lacking_the_field_are_skipped_not_assumed() -> None:
    # cells()/column() skip rows without the field: a candidate of other fields is "no values".
    v = DA.check(column("reserve", grid(100, 9)), amount_history(40), HEALED)
    assert v.outcome is Outcome.INSUFFICIENT and "no amount values" in v.reason


@pytest.mark.unit
def test_a_reference_window_without_the_field_is_insufficient() -> None:
    h = history_of("reserve", 40, values=lambda i: grid(100, 9))
    v = DA.check(SAME, h, HEALED)
    assert v.outcome is Outcome.INSUFFICIENT and "reference window" in v.reason


@pytest.mark.unit
def test_a_tiny_current_sample_is_not_evidence() -> None:
    # Same thinking as Task 22's MIN_JOINED: PSI over a handful of values is noise either way.
    few = column("amount", grid(101, 9, n=MIN_VALUES - 1))
    v = DA.check(few, amount_history(40), HEALED)
    assert v.outcome is Outcome.INSUFFICIENT and f"fewer than {MIN_VALUES}" in v.reason
    enough = column("amount", grid(5000, 9, n=MIN_VALUES))
    assert DA.check(enough, amount_history(40), HEALED).outcome is Outcome.FAIL


@pytest.mark.unit
def test_a_tiny_reference_window_is_not_evidence() -> None:
    h = history_of("amount", 40, values=lambda i: grid(100, 9, n=1 if i % 5 == 0 else 0))
    v = DA.check(SAME, h, HEALED)  # 6 reference values across the trailing 30 runs
    assert v.outcome is Outcome.INSUFFICIENT and f"fewer than {MIN_VALUES}" in v.reason


@pytest.mark.unit
def test_hostile_cell_text_never_raises_and_is_never_echoed() -> None:
    hostile = ["<script>alert(1)</script>", "9" * 5000, "nan", "inf", "\x00", "١٢٣"] * 20
    v = DA.check(column("amount", hostile), amount_history(40), HEALED)
    assert v.outcome is Outcome.FAIL
    assert "script" not in v.reason and "999999" not in v.reason


@pytest.mark.unit
def test_assess_and_psi_of_values_agree_so_task_29_cannot_print_a_second_psi() -> None:
    h = amount_history(40)
    a = DA.assess(SHIFTED, h, "amount")
    reference = h.trailing(REFERENCE_RUNS).values("amount")
    assert isinstance(a, Assessment)
    assert (a.psi, a.categorical) == psi_of_values(reference, grid(5000, 9))
    assert (a.reference_n, a.current_n) == (3000, 100)


@pytest.mark.unit
@pytest.mark.parametrize(
    "kw",
    [
        {"warmup_runs": 0},
        {"bins": 1},
        {"threshold": 0.0},
        {"persistence": 0},
        {"epsilon": 0.0},
        {"threshold": math.nan},
        {"threshold": math.inf},
        {"epsilon": math.nan},
        {"epsilon": math.inf},
    ],
)
def test_parameters_that_would_disable_the_check_are_refused(kw: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        DistributionalAssertion(**kw)  # type: ignore[arg-type]


# -- through the gate: the asymmetry the brief's table depends on ------------------------------


@pytest.mark.unit
def test_through_the_gate_warmup_quarantines_a_heal_but_not_a_cache_hit() -> None:
    checks = {"amount": [DA]}
    healed = gate_run(SAME, amount_history(5), {"amount": HEALED}, checks)
    steady = gate_run(SAME, amount_history(5), {"amount": STEADY}, checks)
    assert isinstance(healed, QuarantinedRun)
    assert isinstance(steady, ValidatedExtraction)


@pytest.mark.unit
def test_through_the_gate_a_persistent_shift_on_a_cache_hit_still_writes() -> None:
    r = gate_run(
        SHIFTED, amount_history(40, prior_breaches=1), {"amount": STEADY}, {"amount": [DA]}
    )
    assert isinstance(r, ValidatedExtraction)
    r = gate_run(SHIFTED, amount_history(40), {"amount": HEALED}, {"amount": [DA]})
    assert isinstance(r, QuarantinedRun)


# -- the brief's synthetic data-quality tests (CLAUDE.md testing bar) ---------------------------


@pytest.mark.unit
def test_synthetic_shift_is_detected_after_warmup() -> None:
    # A heal onto the adjacent column: every value valid, the distribution moved (x1.6).
    h = amount_history(40)
    adjacent = column("amount", grid(160, 14.4))
    v = DA.check(adjacent, h, HEALED)
    assert v.outcome is Outcome.FAIL


@pytest.mark.unit
def test_synthetic_seasonality_is_not_alerted_after_warmup() -> None:
    # A month-end spike: one shifted run, then normal again. It never alerts in steady state,
    # and the next normal run is clean.
    h = amount_history(40)
    spike = DA.assess(SHIFTED, h, "amount")
    assert isinstance(spike, Assessment) and spike.breach and not spike.alert
    after = History(
        runs=(*h.runs[1:], dataclasses.replace(h.runs[-1], shift_alerts=frozenset({"amount"})))
    )
    back = DA.assess(SAME, after, "amount")
    assert isinstance(back, Assessment) and not back.breach and not back.alert
