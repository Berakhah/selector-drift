from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Sequence

import pytest

from testbench.portal import data
from testbench.portal.data import (
    COLUMNS,
    EPOCH,
    FIRST_NAMES,
    LAST_NAMES,
    Claim,
    Status,
    dataset_for,
)
from tests.testbench.helpers import CLAIM_NUMBER, FIXTURE_DAY

ROW_KEY_CONTINUITY_THRESHOLD = 0.95  # RowKeyContinuity's default (Task 22)


def agreement(before: Sequence[Claim], after: Sequence[Claim], field: str) -> float:
    old = {c.claim_number: c for c in before}
    new = {c.claim_number: c for c in after}
    common = old.keys() & new.keys()
    return sum(getattr(old[k], field) == getattr(new[k], field) for k in common) / len(common)


@pytest.mark.unit
def test_the_base_set_is_five_hundred_synthetic_claims() -> None:
    base = dataset_for(EPOCH)
    assert len(base) == 500
    assert len({c.claim_number for c in base}) == 500
    for c in base:
        first, last = c.claimant.split(" ")
        assert first in FIRST_NAMES and last in LAST_NAMES


@pytest.mark.unit
@pytest.mark.parametrize("day", [EPOCH, FIXTURE_DAY, dt.date(2027, 3, 1)], ids=str)
def test_every_claim_is_well_formed(day: dt.date) -> None:
    for c in dataset_for(day):
        assert CLAIM_NUMBER.fullmatch(c.claim_number)
        assert data.MIN_MONEY <= c.amount <= data.MAX_MONEY
        assert data.MIN_MONEY <= c.reserve <= data.MAX_MONEY
        assert c.amount == c.amount.quantize(data.CENT)
        assert c.status in Status
        assert day - data.RETENTION <= c.filed <= day


@pytest.mark.unit
def test_the_dataset_for_a_day_is_reproducible() -> None:
    """Any day's page is reproducible from the date alone (bench spec §4): from scratch and
    by stepping from a cached earlier day, the same claims."""
    stepped = dataset_for(FIXTURE_DAY)
    data.clear_cache()
    from_scratch = dataset_for(FIXTURE_DAY)
    assert stepped == from_scratch and stepped is not from_scratch
    data.clear_cache()
    dataset_for(FIXTURE_DAY - dt.timedelta(days=40))
    assert dataset_for(FIXTURE_DAY) == from_scratch


@pytest.mark.unit
def test_a_day_changes_the_dataset_a_little() -> None:
    before, after = dataset_for(FIXTURE_DAY), dataset_for(FIXTURE_DAY + dt.timedelta(days=1))
    new_keys = {c.claim_number for c in after} - {c.claim_number for c in before}
    assert 3 <= len(new_keys) <= 7
    assert before != after


@pytest.mark.unit
def test_benign_daily_delta_keeps_key_agreement_above_threshold() -> None:
    """On a day with no mutation, RowKeyContinuity must see ~98% of shared keys agree --
    above its 0.95 threshold -- for every field, for 90 consecutive days. Otherwise the bench's
    own data churn would quarantine benign heals and the false-positive rate would be ours."""
    start = dt.date(2026, 9, 1)
    for offset in range(90):
        day = start + dt.timedelta(days=offset)
        before, after = dataset_for(day), dataset_for(day + dt.timedelta(days=1))
        for key, _ in COLUMNS[1:]:
            assert agreement(before, after, key) >= ROW_KEY_CONTINUITY_THRESHOLD, (day, key)


@pytest.mark.unit
def test_amount_and_reserve_have_no_arithmetic_relation() -> None:
    """Bench spec §4: no footing rule relates the two, so a CrossFieldAssertion has nothing to
    check and ValueSwap (Task 11A) is invisible to it by construction."""
    claims = dataset_for(FIXTURE_DAY)
    amounts = [float(c.amount) for c in claims]
    reserves = [float(c.reserve) for c in claims]
    n = len(claims)
    assert all(c.amount != c.reserve for c in claims)
    assert len({round(r / a, 6) for a, r in zip(amounts, reserves, strict=True)}) > 0.9 * n
    assert len({round(r - a, 2) for a, r in zip(amounts, reserves, strict=True)}) > 0.9 * n
    assert len({round(r + a, 2) for a, r in zip(amounts, reserves, strict=True)}) > 0.9 * n
    log_a = [math.log(a) for a in amounts]
    log_r = [math.log(r) for r in reserves]
    assert abs(statistics.correlation(log_a, log_r)) < 0.15


@pytest.mark.unit
def test_days_before_the_epoch_are_refused() -> None:
    with pytest.raises(ValueError, match="begins on"):
        dataset_for(EPOCH - dt.timedelta(days=1))
