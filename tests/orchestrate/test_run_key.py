from __future__ import annotations

import datetime as dt

import pytest

from selector_drift.orchestrate.run_record import parse_run_key, run_key, window_for


@pytest.mark.unit
def test_run_key_format_is_target_version_window() -> None:
    assert run_key("testbench", 2, "2026-09-07") == "testbench:2:2026-09-07"


@pytest.mark.unit
@pytest.mark.parametrize("target", ["a:b", "", "TestBench"])
def test_run_key_refuses_a_target_that_would_break_the_format(target: str) -> None:
    with pytest.raises(ValueError, match="target"):
        run_key(target, 1, "2026-09-07")


@pytest.mark.unit
@pytest.mark.parametrize("window", ["2026-9-7", "20260907", "2026-09-07T04:12"])
def test_run_key_refuses_a_window_that_is_not_an_iso_date(window: str) -> None:
    with pytest.raises(ValueError, match="window"):
        run_key("testbench", 1, window)


@pytest.mark.unit
def test_parse_run_key_round_trips() -> None:
    assert parse_run_key("ledger-sense:3:2026-09-07") == ("ledger-sense", 3, dt.date(2026, 9, 7))


@pytest.mark.unit
def test_the_window_is_the_utc_date_of_the_slot() -> None:
    """M20. 23:30 in UTC-5 is already tomorrow in UTC; the window follows UTC."""
    slot = dt.datetime(2026, 9, 7, 23, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    assert window_for(slot) == "2026-09-08"


@pytest.mark.unit
def test_a_naive_slot_is_refused() -> None:
    with pytest.raises(ValueError, match="UTC"):
        window_for(dt.datetime(2026, 9, 7, 4, 12))
