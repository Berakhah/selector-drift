from __future__ import annotations

import datetime as dt

import pytest

from selector_drift.db.models import Resolution, Run
from selector_drift.db.session import Session
from selector_drift.orchestrate.rung_stats import rung_stats

AT = dt.datetime(2026, 9, 7, 4, 12, tzinfo=dt.UTC)
KEY = "testbench:1:2026-09-07"


@pytest.mark.integration
def test_rung_stats_is_empty_before_any_resolution(db) -> None:
    assert rung_stats() == {}


@pytest.mark.integration
def test_the_denominator_is_every_resolution_including_unresolved(db) -> None:
    """C4/B11. A resolution no rung won still counts; otherwise the published shares inflate."""
    with Session() as s:
        s.add(
            Run(
                run_key=KEY,
                target="testbench",
                spec_version=1,
                window=dt.date(2026, 9, 7),
                first_started_at=AT,
                state="completed",
            )
        )
        s.flush()
        for rung in (1, 1, 3, None):
            s.add(
                Resolution(
                    run_key=KEY,
                    target="testbench",
                    field="claim_number",
                    winning_rung=rung,
                    attempts=[],
                    node_id="n1" if rung else None,
                    ms=3,
                    at=AT,
                )
            )
        s.commit()
    assert rung_stats() == {"rung1": 0.5, "rung2": 0.0, "rung3": 0.25, "rung4": 0.0, "rung5": 0.0}
