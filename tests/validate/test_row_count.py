"""A run that lost most of its rows is held before it lands, not alerted about afterwards.

`quality.contracts._row_count` already flags a short run -- but after `accept()` has written it,
and a page-1-only pagination failure or a virtualised table returns correctly placed, correctly
formatted rows that every per-field check passes. This check judges the run's size against the
trailing median of accepted runs, on a cache hit as much as on a heal (the markup is untouched
when a row scope silently shrinks).

Callers: none (pytest collects tests/validate/test_*.py). Instruction: "i like if go".
"""

import datetime as dt

import pytest

from selector_drift.quality import contracts
from selector_drift.scope.record import FirstHealPolicy
from selector_drift.spec.schema import TaskSpec
from selector_drift.validate.assertions import row_count
from selector_drift.validate.assertions.row_count import RowCountContinuity
from selector_drift.validate.checks import checks_for
from selector_drift.validate.gate import ValidatedExtraction, gate
from selector_drift.validate.history import AcceptedRun, History
from selector_drift.validate.quarantine import QuarantinedRun
from selector_drift.validate.verdict import Outcome
from tests.validate.helpers import AMOUNT, column, ctx


def history_with_counts(counts: list[int]) -> History:
    """One accepted run per count, oldest first, each with that many rows."""
    runs = []
    for i, n in enumerate(counts):
        day = dt.date(2026, 10, 1) - dt.timedelta(days=len(counts) - i)
        rows = tuple({"claim_number": f"AB{j:08d}", "amount": "1.00"} for j in range(n))
        runs.append(
            AcceptedRun(
                run_key=f"testbench:2:{day}",
                window=day,
                rows=rows,
                natural_key=("claim_number",),
            )
        )
    return History(runs=tuple(runs))


def run_of(n: int):
    return column("amount", ["1.00"] * n)


def verdict(n: int, counts: list[int], *, healed: bool = False):
    return RowCountContinuity().check(
        run_of(n), history_with_counts(counts), ctx(AMOUNT, healed=healed, rung=3 if healed else 1)
    )


@pytest.mark.unit
def test_a_run_below_half_the_trailing_median_fails():
    v = verdict(40, [100, 100, 100, 100, 100])
    assert v.outcome is Outcome.FAIL
    assert v.observed is not None and "40" in v.observed and "100" in v.observed


@pytest.mark.unit
def test_exactly_half_the_median_passes():
    assert verdict(50, [100, 100, 100]).outcome is Outcome.PASS


@pytest.mark.unit
def test_a_larger_run_passes():
    assert verdict(180, [100, 100, 100]).outcome is Outcome.PASS


@pytest.mark.unit
def test_an_empty_run_fails_against_a_non_empty_history():
    assert verdict(0, [100, 100, 100]).outcome is Outcome.FAIL


@pytest.mark.unit
def test_the_median_not_the_mean_sets_the_bar():
    """One month-end spike must not raise the bar: mean 280, median 100, a run of 60 is fine."""
    assert verdict(60, [100, 100, 100, 100, 1000]).outcome is Outcome.PASS


@pytest.mark.unit
@pytest.mark.parametrize("counts", [[], [100], [100, 100]])
def test_fewer_than_three_accepted_runs_is_no_basis_not_a_pass(counts):
    assert verdict(5, counts).outcome is Outcome.INSUFFICIENT


@pytest.mark.unit
def test_no_history_is_no_basis():
    v = RowCountContinuity().check(run_of(5), None, ctx(AMOUNT, healed=False, rung=1))
    assert v.outcome is Outcome.INSUFFICIENT


@pytest.mark.unit
def test_only_the_trailing_window_counts():
    """An old, much larger run outside the reference window does not set today's bar."""
    old = [10_000] * 5
    recent = [100] * row_count.REFERENCE_RUNS
    assert verdict(60, old + recent).outcome is Outcome.PASS


@pytest.mark.unit
def test_the_verdict_carries_counts_and_never_a_cell_value():
    v = RowCountContinuity().check(
        column("amount", ["SECRET-123"] * 3),
        history_with_counts([100, 100, 100]),
        ctx(AMOUNT, healed=False, rung=1),
    )
    text = " ".join(str(x) for x in (v.observed, v.reason, v.threshold))
    assert "SECRET" not in text


@pytest.mark.unit
def test_a_short_run_is_quarantined_even_on_a_cache_hit():
    """The markup is untouched (rung 1); a shrunken row scope is only visible as a short run."""
    result = gate(
        run_of(40),
        history_with_counts([100, 100, 100]),
        ctx(AMOUNT, healed=False, rung=1),
        [RowCountContinuity()],
    )
    assert isinstance(result, QuarantinedRun)
    assert "RowCountContinuity" in result.reason


@pytest.mark.unit
def test_a_run_of_normal_size_is_still_accepted():
    result = gate(
        run_of(100),
        history_with_counts([100, 100, 100]),
        ctx(AMOUNT, healed=False, rung=1),
        [RowCountContinuity()],
    )
    assert isinstance(result, ValidatedExtraction)


@pytest.mark.unit
def test_a_healed_run_with_no_basis_is_held_but_a_cache_hit_is_tolerated():
    """The gate's asymmetry (design 5.7): INSUFFICIENT fails a heal and passes a cache hit."""
    healed = gate(run_of(5), None, ctx(AMOUNT, healed=True, rung=3), [RowCountContinuity()])
    hit = gate(run_of(5), None, ctx(AMOUNT, healed=False, rung=1), [RowCountContinuity()])
    assert isinstance(healed, QuarantinedRun)
    assert isinstance(hit, ValidatedExtraction)


SPEC = TaskSpec.model_validate(
    {
        "version": 2,
        "scope_ref": "scopes/testbench.yaml",
        "schedule": "12 4 * * *",
        "row_scope": "each row",
        "natural_key": ["claim_number"],
        "expected_schema": {"claim_number": "string", "amount": "decimal"},
        "navigate": [],
        "targets": [
            {"name": "claim_number", "description": "d", "assertions": []},
            {"name": "amount", "description": "d", "distributional": True, "assertions": []},
        ],
    }
)


def names_for(field):
    return [
        type(c).__name__
        for c in checks_for(
            SPEC, field, [], warmup_runs=5, first_heal_policy=FirstHealPolicy.REQUIRED
        )
    ]


@pytest.mark.unit
def test_exactly_one_field_carries_the_run_level_check():
    """The check judges the run, not a field: on every field it would print N identical
    verdicts into the heal log."""
    carrying = [f.name for f in SPEC.targets if "RowCountContinuity" in names_for(f)]
    assert carrying == [SPEC.targets[0].name]


@pytest.mark.unit
def test_the_gate_and_the_post_write_rule_share_one_threshold():
    assert contracts.ROW_COUNT_RATIO == row_count.ROW_COUNT_RATIO == 0.5
    assert contracts.ROW_COUNT_MIN_WINDOWS == row_count.ROW_COUNT_MIN_WINDOWS == 3
