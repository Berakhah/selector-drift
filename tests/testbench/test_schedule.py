from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from testbench.mutations.base import apply_all
from testbench.schedule import mutations_for, scheduled_names, seed_for
from tests.testbench.helpers import TagBody, bench_catalogue

CATALOGUE = bench_catalogue()
BENIGN = {e.name for e in CATALOGUE if not e.adversarial}
HOSTILE = {e.name for e in CATALOGUE if e.adversarial}


@pytest.mark.unit
def test_schedule_is_deterministic_for_a_day() -> None:
    a = scheduled_names(dt.date(2026, 9, 9), "testbench", CATALOGUE)
    b = scheduled_names(dt.date(2026, 9, 9), "testbench", CATALOGUE)
    assert a == b


@pytest.mark.unit
def test_different_days_differ() -> None:
    assert seed_for(dt.date(2026, 9, 9), "testbench") != seed_for(dt.date(2026, 9, 10), "testbench")
    start = dt.date(2026, 9, 1)
    schedules = {
        tuple(scheduled_names(start + dt.timedelta(days=i), "testbench", CATALOGUE))
        for i in range(30)
    }
    assert len(schedules) >= 10


@pytest.mark.unit
def test_the_seed_is_sha256_of_day_and_target() -> None:
    expected = hashlib.sha256(b"2026-09-09:testbench").hexdigest()
    assert seed_for(dt.date(2026, 9, 9), "testbench") == expected


@pytest.mark.unit
def test_every_day_applies_one_or_two_benign_mutations_first() -> None:
    for i in range(120):
        names = scheduled_names(dt.date(2026, 9, 1) + dt.timedelta(days=i), "testbench", CATALOGUE)
        benign = [n for n in names if n in BENIGN]
        assert 1 <= len(benign) <= 2
        assert names[: len(benign)] == benign  # adversarial, if any, is applied last


@pytest.mark.unit
def test_adversarial_mutations_are_scheduled_about_weekly() -> None:
    days = [dt.date(2026, 9, 1) + dt.timedelta(days=i) for i in range(365)]
    hostile_days = sum(
        any(n in HOSTILE for n in scheduled_names(d, "testbench", CATALOGUE)) for d in days
    )
    assert 25 <= hostile_days <= 85  # p = 1/7 -> ~52


@pytest.mark.unit
def test_an_empty_catalogue_schedules_nothing() -> None:
    assert mutations_for(dt.date(2026, 9, 9), "testbench", ()) == []


@pytest.mark.unit
def test_apply_all_applies_in_order() -> None:
    html = "<!DOCTYPE html><html><head></head><body></body></html>"
    out = apply_all([TagBody("A"), TagBody("B")], html)
    assert 'data-test-a="1"' in out and 'data-test-b="1"' in out
    assert apply_all([], html) == html
