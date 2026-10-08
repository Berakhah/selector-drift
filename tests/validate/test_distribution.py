from __future__ import annotations

import dataclasses
import datetime as dt
import math

import pytest

from selector_drift.scope.gate import authorise
from selector_drift.scope.record import AuthorisedTarget, Basis, ScopeRecord
from selector_drift.validate.assertions.base import Assertion
from selector_drift.validate.assertions.distribution import (
    ALPHA,
    MIN_VALUES,
    REFERENCE_RUNS,
    Assessment,
    DistributionalAssertion,
    Judgement,
)
from selector_drift.validate.gate import QuarantinedRun, ValidatedExtraction, gate_run
from selector_drift.validate.history import History
from selector_drift.validate.stats import (
    MIN_EXPECTED_PER_BIN,
    PSI_BINS,
    critical_psi,
    psi_of_values,
    psi_result,
)
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
    field_target,
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
    # M16. PSI is undefined at log(0). Without smoothing this silently returns inf,
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
    # A PASS from a check with no power is fail-open: ten values wholly outside the reference
    # range are not a PASS, they are no basis -- a heal quarantines (fix round 3).
    outside = column("amount", grid(5000, 9, n=10))
    v = DA.check(outside, amount_history(40), HEALED)
    assert v.outcome is Outcome.INSUFFICIENT and f"fewer than {MIN_VALUES}" in v.reason
    steady = DA.check(outside, amount_history(40, prior_breaches=1), STEADY)
    assert steady.outcome is Outcome.INSUFFICIENT  # tolerated on a cache hit; no alert
    assert isinstance(DA.assess(outside, amount_history(40), "amount"), str)


@pytest.mark.unit
def test_min_values_is_set_from_measured_power() -> None:
    # The smallest n at which BOTH a x10 and a x3 shift are detected >= 95% of the time on
    # scripts/measure_psi_power.py, rounded up to a multiple of 10: n=40 (x3 98.0%; n=35 93.7%).
    # x10 alone gave 20 once the overflow bin existed, with x3 at 49% (fix round 2).
    assert MIN_VALUES == 40


@pytest.mark.unit
def test_min_values_boundary() -> None:
    below = column("amount", grid(5000, 9, n=MIN_VALUES - 1))
    at = column("amount", grid(5000, 9, n=MIN_VALUES))
    assert DA.check(below, amount_history(40), HEALED).outcome is Outcome.INSUFFICIENT
    v = DA.check(at, amount_history(40), HEALED)
    assert v.outcome is Outcome.FAIL and v.observed is not None  # judged, and wholly outside


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
        {"threshold": math.nan},
        {"threshold": math.inf},
        {"alpha": 0.0},
        {"alpha": 1.0},
        {"alpha": -0.01},
        {"alpha": math.nan},
        {"alpha": 1e-300},
        {"threshold": 1.5},
        {"threshold": -0.1},
        {"bins": 11},
        {"bins": 0},
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


# -- fix round 1: the chi-square noise floor (concern 1) and the parse floor (concern 2) --------


def _bunched() -> list[str]:
    """40 values against the 100..991 reference, five in each of the eight lowest deciles and
    none in the top two: over 0.20, and under the chi-square critical value."""
    return [v for d in range(8) for v in grid(110 + 89 * d, 1, n=5)]


@pytest.mark.unit
def test_alpha_is_published() -> None:
    assert ALPHA == 0.01 and DA.alpha == 0.01


@pytest.mark.unit
def test_small_sample_noise_over_threshold_but_under_the_critical_value_is_not_a_breach() -> None:
    h = amount_history(40)
    small = column("amount", _bunched())
    a = DA.assess(small, h, "amount")
    assert isinstance(a, Assessment)
    assert 0.20 < a.psi < a.critical and not a.breach
    v = DA.check(small, h, HEALED)
    assert v.outcome is Outcome.PASS
    assert "threshold 0.20" in v.reason and f"critical {a.critical:.4f}" in v.reason


@pytest.mark.unit
def test_the_critical_value_is_the_one_stats_computes_for_the_effective_bins() -> None:
    h = amount_history(40)
    a = DA.assess(SAME, h, "amount")
    r = psi_result(h.trailing(REFERENCE_RUNS).values("amount"), grid(101, 9))
    assert isinstance(a, Assessment)
    assert a.critical == pytest.approx(critical_psi(r.k, 3000, 100, ALPHA))


@pytest.mark.unit
def test_a_large_shift_still_breaches_the_larger_of_threshold_and_critical() -> None:
    a = DA.assess(SHIFTED, amount_history(40), "amount")
    assert isinstance(a, Assessment) and a.breach and a.psi > max(0.20, a.critical)
    v = DA.check(SHIFTED, amount_history(40), HEALED)
    assert "critical" in v.reason and "threshold 0.20" in v.reason


@pytest.mark.unit
def test_steady_state_alerting_uses_the_same_bar() -> None:
    small = column("amount", _bunched())
    a = DA.assess(small, amount_history(40, prior_breaches=1), "amount")
    assert isinstance(a, Assessment) and not a.breach and not a.alert


@pytest.mark.unit
def test_the_categorical_path_uses_the_same_bar() -> None:
    h = history_of("status", 40, values=lambda i: ["Open", "Paid"] * 50)
    a = DA.assess(column("status", ["Closed"] * 100), h, "status")
    assert isinstance(a, Assessment) and a.categorical
    # k = 3 categories, df = 2.
    assert a.critical == pytest.approx(critical_psi(3, 3000, 100, ALPHA)) and a.breach


@pytest.mark.unit
def test_a_constant_usd_field_heals_to_pass() -> None:
    # Ruling 2: one bin or category on both sides is no shift, not "no basis".
    h = history_of("currency", 40, values=lambda i: ["USD"] * 100)
    usd = field_target("currency", distributional=True)
    v = DA.check(column("currency", ["USD"] * 100), h, ctx(usd, rung=3))
    assert v.outcome is Outcome.PASS and float(v.observed or "nan") == 0.0
    a = DA.assess(column("currency", ["USD"] * 100), h, "currency")
    assert isinstance(a, Assessment) and not a.breach
    steady = DA.check(column("currency", ["USD"] * 100), h, ctx(usd, healed=False, rung=1))
    assert steady.outcome is Outcome.PASS and "alert" not in steady.reason


@pytest.mark.unit
def test_a_heal_onto_a_different_constant_is_not_a_pass() -> None:
    h = history_of("currency", 40, values=lambda i: ["USD"] * 100)
    usd = field_target("currency", distributional=True)
    a = DA.assess(column("currency", ["EUR"] * 100), h, "currency")
    assert isinstance(a, Assessment) and a.breach  # k = 2: the normal rule
    assert DA.check(column("currency", ["EUR"] * 100), h, ctx(usd, rung=3)).outcome is Outcome.FAIL


@pytest.mark.unit
def test_a_constant_numeric_field_same_bin_passes_and_a_different_bin_fails() -> None:
    h = history_of("amount", 40, values=lambda i: ["100.00"] * 100)
    same = DA.check(column("amount", ["100.00"] * 100), h, HEALED)
    moved = DA.check(column("amount", ["200.00"] * 100), h, HEALED)
    assert same.outcome is Outcome.PASS and moved.outcome is Outcome.FAIL


@pytest.mark.unit
def test_one_na_in_a_numeric_reference_keeps_the_field_numeric_and_a_matching_heal_passes() -> None:
    def values(i: int) -> list[str]:
        g = grid(100 + (i % 3), 9)
        return [*g[:-1], "N/A"] if i == 39 else g

    h = history_of("amount", 40, values=values)
    a = DA.assess(SAME, h, "amount")
    assert isinstance(a, Assessment) and not a.categorical
    assert DA.check(SAME, h, HEALED).outcome is Outcome.PASS
    assert DA.bin_edges_for(h, "amount") != ()


@pytest.mark.unit
def test_the_constant_field_pass_still_needs_min_values() -> None:
    h = history_of("currency", 40, values=lambda i: ["USD"] * 100)
    usd = field_target("currency", distributional=True)
    few = DA.check(column("currency", ["USD"] * (MIN_VALUES - 1)), h, ctx(usd, rung=3))
    assert few.outcome is Outcome.INSUFFICIENT
    enough = DA.check(column("currency", ["USD"] * MIN_VALUES), h, ctx(usd, rung=3))
    assert enough.outcome is Outcome.PASS


# -- fix round 1 (review a9318221f39156743) -----------------------------------------------------


@pytest.mark.unit
def test_the_widest_legal_parameters_are_accepted() -> None:
    # M5: threshold in (0, 1]; bins in [2, PSI_BINS] -- MIN_VALUES was measured at PSI_BINS.
    DistributionalAssertion(threshold=1.0, bins=2)
    DistributionalAssertion(bins=PSI_BINS)


@pytest.mark.unit
@pytest.mark.parametrize(
    "current",
    [[f"{i}.00" for i in range(100)], ["50.00"] * 100],
    ids=["0..99", "constant-50"],
)
def test_a_heal_from_a_constant_onto_lower_values_fails(current: list[str]) -> None:
    # C1: a constant 100.00 reference; the heal lands on values at or below it.
    h = history_of("amount", 40, values=lambda i: ["100.00"] * 100)
    v = DA.check(column("amount", current), h, HEALED)
    assert v.outcome is Outcome.FAIL
    a = DA.assess(column("amount", current), h, "amount")
    assert isinstance(a, Assessment) and a.breach


@pytest.mark.unit
def test_a_heal_from_a_constant_onto_the_same_constant_still_passes() -> None:
    h = history_of("amount", 40, values=lambda i: ["100.00"] * 100)
    assert DA.check(column("amount", ["100.00"] * 100), h, HEALED).outcome is Outcome.PASS


@pytest.mark.unit
def test_a_heal_wholly_above_the_reference_range_fails_via_the_overflow_bin() -> None:
    h = amount_history(40)  # 100..993
    a = DA.assess(column("amount", grid(994, 0.01)), h, "amount")  # just above max
    assert isinstance(a, Assessment) and a.breach


def _codes(i: int) -> list[str]:
    return [f"C{(30 * i + j) % 900:03d}" for j in range(30)]


@pytest.mark.unit
def test_a_disjoint_heal_on_a_900_category_field_is_insufficient_not_pass() -> None:
    # C2(a): k categories need ~5 expected per category for the chi-square floor to mean
    # anything. 1000 categories against 100 current values is no basis -- never a PASS.
    h = history_of("code", 40, values=_codes)
    code = field_target("code", distributional=True)
    current = column("code", [f"D<b>{j:03d}" for j in range(100)])
    v = DA.check(current, h, ctx(code, rung=3))
    assert v.outcome is Outcome.INSUFFICIENT
    assert "1000 categories" in v.reason and "100 code values" in v.reason
    assert f"{MIN_EXPECTED_PER_BIN}" in v.reason
    assert "<b>" not in v.reason and "C000" not in v.reason  # never cell text
    assert isinstance(DA.assess(current, h, "code"), str)


@pytest.mark.unit
def test_the_expected_count_floor_boundary() -> None:
    # 12 categories (6 in the reference, 6 new): judged at 5 * 12 = 60 current values, not at 59.
    old = ["Open", "Paid", "Closed", "Void", "Held", "Sent"]
    new = ["Draft", "Lost", "Moved", "Merged", "Split", "Queued"]
    h = history_of("status", 40, values=lambda i: old * 10)
    status = field_target("status", distributional=True)
    at = column("status", (new * 10)[: MIN_EXPECTED_PER_BIN * 12])
    below = column("status", (new * 10)[: MIN_EXPECTED_PER_BIN * 12 - 1])
    assert MIN_EXPECTED_PER_BIN * 12 - 1 >= MIN_VALUES  # the floor, not MIN_VALUES, refuses
    assert DA.check(at, h, ctx(status, rung=3)).outcome is Outcome.FAIL
    v = DA.check(below, h, ctx(status, rung=3))
    assert v.outcome is Outcome.INSUFFICIENT and "12 categories" in v.reason


def _enum(i: int) -> list[str]:
    return ["Open"] * 70 + ["Paid"] * 20 + ["Closed"] * 5 + ["Void"] * 5


@pytest.mark.unit
def test_a_small_enum_still_judges_and_a_flipped_enum_fails() -> None:
    h = history_of("status", 40, values=_enum)
    status = field_target("status", distributional=True)
    same = column("status", ["Open"] * 35 + ["Paid"] * 10 + ["Closed"] * 3 + ["Void"] * 2)
    flipped = column("status", ["Open"] * 2 + ["Paid"] * 3 + ["Closed"] * 10 + ["Void"] * 35)
    assert DA.check(same, h, ctx(status, rung=3)).outcome is Outcome.PASS
    v = DA.check(flipped, h, ctx(status, rung=3))
    assert v.outcome is Outcome.FAIL and "category proportions" in v.reason


def _blank_amounts(i: int) -> list[str]:
    # Every run's amounts distinct (as real amounts are), 6 of 100 cells blank.
    g = grid(100 + 0.37 * i, 9)
    return [("" if j % 17 == 0 else v) for j, v in enumerate(g)]


@pytest.mark.unit
def test_six_percent_blank_amounts_stay_numeric_and_a_matching_heal_passes() -> None:
    h = history_of("amount", 40, values=_blank_amounts)
    a = DA.assess(column("amount", _blank_amounts(1)), h, "amount")
    assert isinstance(a, Assessment) and not a.categorical and not a.breach


@pytest.mark.unit
def test_six_percent_blank_amounts_healed_onto_ten_times_the_values_fails() -> None:
    # C2(b): measured PSI 0.39 vs critical 31.5 -> PASS while blanks made the field categorical.
    h = history_of("amount", 40, values=_blank_amounts)
    x10 = [("" if j % 17 == 0 else f"{10 * (101 + 9 * j):,.2f}") for j in range(100)]
    v = DA.check(column("amount", x10), h, HEALED)
    assert v.outcome is Outcome.FAIL


@pytest.mark.unit
def test_six_percent_blank_amounts_healed_onto_an_na_column_fails() -> None:
    h = history_of("amount", 40, values=_blank_amounts)
    v = DA.check(column("amount", ["n/a"] * 100), h, HEALED)
    assert v.outcome is Outcome.FAIL


@pytest.mark.unit
def test_the_private_judge_is_the_breach_rule_assess_applies() -> None:
    # Fix round 2: _judge() skips warm-up and MIN_VALUES, so it is private; assess() is the only
    # path to a verdict. scripts/measure_psi_power.py measures through _judge() below
    # MIN_VALUES; assess() must be _judge() plus the guards, nothing else.
    assert not hasattr(DistributionalAssertion, "judge")
    h = amount_history(40)
    reference = h.trailing(REFERENCE_RUNS).values("amount")
    for current in (grid(101, 9), grid(5000, 9), _bunched()):
        j = DA._judge(reference, current, "amount")
        a = DA.assess(column("amount", current), h, "amount")
        assert isinstance(j, Judgement) and isinstance(a, Assessment)
        assert (j.result.psi, j.critical, j.breach) == (a.psi, a.critical, a.breach)


# -- fix round 2 (re-review a78cc83b13ce18e8e): blanks get their own bin; thin numeric reference


def _mostly_blank(i: int) -> list[str]:
    """100 cells per run: 90 blank, 10 amounts distinct per run."""
    return ["" if j % 10 else f"{100 + 0.37 * i + 9 * j:,.2f}" for j in range(100)]


@pytest.mark.unit
def test_a_mostly_blank_field_healed_onto_a_text_column_fails() -> None:
    h = history_of("amount", 40, values=_mostly_blank)
    # 400 cells, 10% text: MIN_VALUES non-blank cells, so judged (fix round 3 sized it up).
    current = column("amount", ["see notes" if j % 10 == 0 else "" for j in range(400)])
    v = DA.check(current, h, HEALED)
    assert v.outcome is Outcome.FAIL and "see notes" not in v.reason


@pytest.mark.unit
def test_a_mostly_blank_field_healed_onto_the_same_mix_passes() -> None:
    h = history_of("amount", 40, values=_mostly_blank)
    # Five runs' worth of cells: 50 amounts among 450 blanks, so judged (fix round 3 sized it up).
    same_mix = [cell for i in range(41, 46) for cell in _mostly_blank(i)]
    assert DA.check(column("amount", same_mix), h, HEALED).outcome is Outcome.PASS


def _thin(numbers: int) -> History:
    """40 runs of 20 blank cells; the trailing 30 runs also hold `numbers` amounts between them."""

    def values(i: int) -> list[str]:
        own = [f"{100 + 9 * j:,.2f}" for j in range(numbers) if i >= 10 and j % 30 == i - 10]
        return [""] * 20 + own

    return history_of("amount", 40, values=values)


@pytest.mark.unit
def test_a_numeric_reference_with_one_number_is_no_basis() -> None:
    # 599 blanks + one number: the field is numeric, but quantile edges from one number are
    # degenerate. Fewer than MIN_VALUES parseable reference values -> INSUFFICIENT.
    h = _thin(1)
    assert len(h.trailing(REFERENCE_RUNS).values("amount")) == 601
    v = DA.check(column("amount", ["n/a"] * 100), h, HEALED)
    assert v.outcome is Outcome.INSUFFICIENT
    assert "1 parseable amount values" in v.reason and f"fewer than {MIN_VALUES}" in v.reason


@pytest.mark.unit
def test_the_parseable_reference_floor_boundary() -> None:
    below = DA.check(column("amount", ["n/a"] * 100), _thin(MIN_VALUES - 1), HEALED)
    assert below.outcome is Outcome.INSUFFICIENT and "parseable" in below.reason
    at = DA.check(column("amount", ["n/a"] * 100), _thin(MIN_VALUES), HEALED)
    assert at.outcome is Outcome.FAIL  # judged: a column of text where blanks and amounts were


# -- fix round 3 (re-review a814b0eb4285043dd): the current side counts non-blank cells ---------


@pytest.mark.unit
def test_ten_amounts_among_blanks_are_no_basis_even_shifted() -> None:
    # Measured: 90 blanks + 10 amounts x3 breached on 6.3% of trials -- a PASS with no power.
    h = history_of("amount", 40, values=_mostly_blank)
    x3 = [("" if j % 10 else f"{3 * (100 + 9 * j):,.2f}") for j in range(100)]
    v = DA.check(column("amount", x3), h, HEALED)
    assert v.outcome is Outcome.INSUFFICIENT
    assert "10 non-blank amount values" in v.reason and f"fewer than {MIN_VALUES}" in v.reason


@pytest.mark.unit
def test_one_wild_value_among_blanks_is_no_basis() -> None:
    # Measured: PSI 0.105 against critical 0.256 -- a heal PASSed on one number.
    h = history_of("amount", 40, values=_mostly_blank)
    v = DA.check(column("amount", [""] * 99 + ["98,765,432.10"]), h, HEALED)
    assert v.outcome is Outcome.INSUFFICIENT
    assert "1 non-blank amount values" in v.reason and "98,765" not in v.reason


def _non_blank_among_blanks(n: int) -> list[str]:
    """n non-blank cells -- five of them text, the rest amounts in the reference range -- among
    200 blanks: text counts toward the floor, blanks do not."""
    return [""] * 200 + ["n/a"] * 5 + [f"{100 + 9 * j:,.2f}" for j in range(n - 5)]


@pytest.mark.unit
def test_the_current_non_blank_floor_boundary() -> None:
    h = history_of("amount", 40, values=_mostly_blank)
    below = DA.check(column("amount", _non_blank_among_blanks(MIN_VALUES - 1)), h, HEALED)
    assert below.outcome is Outcome.INSUFFICIENT
    assert f"{MIN_VALUES - 1} non-blank amount values" in below.reason
    at = DA.assess(column("amount", _non_blank_among_blanks(MIN_VALUES)), h, "amount")
    assert isinstance(at, Assessment) and at.current_n == 200 + MIN_VALUES  # judged


@pytest.mark.unit
def test_the_non_blank_floor_does_not_touch_the_categorical_path() -> None:
    # 45 cells, 30 of them blank: a categorical field still judges on its own rules.
    h = history_of("status", 40, values=lambda i: ["Open", "Paid"] * 50)
    status = field_target("status", distributional=True)
    current = column("status", [""] * 30 + ["Open", "Paid"] * 7 + ["Open"])
    a = DA.assess(current, h, "status")
    assert isinstance(a, Assessment) and a.categorical
    assert DA.check(current, h, ctx(status, rung=3)).outcome is not Outcome.INSUFFICIENT
