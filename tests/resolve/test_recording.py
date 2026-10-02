"""C4 against the real table. Postgres, so integration."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, func, select

from selector_drift.db.models import Resolution, Run
from selector_drift.db.session import Session
from selector_drift.resolve import cascade
from selector_drift.resolve.cache import NO_CACHE
from selector_drift.resolve.cascade import resolve_cascade
from selector_drift.resolve.escalation import EscalateToHuman
from selector_drift.resolve.page import StaticPageSource
from selector_drift.resolve.trace import Family
from tests.resolve.conftest import CLAIM_NUMBER
from tests.resolve.test_cascade import PAGE, FakeRung

RUN_KEY = "testbench:2:1999-01-01"  # a window no scheduled run will ever have


@pytest.fixture
def run_row() -> Iterator[str]:
    def clear() -> None:
        with Session() as s:
            s.execute(delete(Resolution).where(Resolution.run_key == RUN_KEY))
            s.execute(delete(Run).where(Run.run_key == RUN_KEY))
            s.commit()

    clear()
    with Session() as s:
        s.add(
            Run(
                run_key=RUN_KEY,
                target="testbench",
                spec_version=2,
                window=dt.date(1999, 1, 1),
                first_started_at=dt.datetime.now(dt.UTC),
                state="completed",
            )
        )
        s.commit()
    yield RUN_KEY
    clear()


@pytest.mark.integration
def test_every_resolution_is_recorded_win_or_lose(run_row: str, monkeypatch) -> None:
    """C4. rung_stats() is a published claim and needs a denominator, not just heals."""
    page = StaticPageSource(PAGE)
    monkeypatch.setattr(cascade, "REGISTERED_RUNGS", [FakeRung(2, Family.TEXT_SEMANTIC, "#real")])
    _, won = resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, run_key=run_row)
    monkeypatch.setattr(cascade, "REGISTERED_RUNGS", [FakeRung(2, Family.TEXT_SEMANTIC, None)])
    with pytest.raises(EscalateToHuman):
        resolve_cascade(page, CLAIM_NUMBER, NO_CACHE, run_key=run_row)
    with Session() as s:
        rows = s.scalars(
            select(Resolution).where(Resolution.run_key == run_row).order_by(Resolution.id)
        ).all()
        assert [r.winning_rung for r in rows] == [2, None]
        assert rows[0].id == won.resolution_id
        assert rows[0].target == "testbench" and rows[0].field == "claim_number"
        assert rows[0].attempts == [
            {
                "rung": 2,
                "family": "TEXT_SEMANTIC",
                "outcome": "resolved",
                "ms": rows[0].attempts[0]["ms"],
            }
        ]
        assert rows[1].node_id is None


@pytest.mark.integration
def test_replay_adds_nothing_to_the_denominator(run_row: str, monkeypatch) -> None:
    monkeypatch.setattr(cascade, "REGISTERED_RUNGS", [FakeRung(2, Family.TEXT_SEMANTIC, "#real")])
    count = select(func.count()).select_from(Resolution)
    with Session() as s:
        before = s.scalar(count)
    resolve_cascade(StaticPageSource(PAGE), CLAIM_NUMBER, NO_CACHE, record=False)
    with Session() as s:
        assert s.scalar(count) == before
