"""Task 26A fix round 1: the audit trail of a human decision can never disagree with what was
written. Promote and discard serialise on the quarantine row; a replayed window never re-opens
a decision a human already took; a quality failure after the rows landed never marks the run
failed (history would drop an accepted run)."""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.models import AuditLog, Quarantine, Run, SelectorCacheEntry
from selector_drift.db.session import Session
from selector_drift.orchestrate import quarantine_actions as qa
from selector_drift.orchestrate.history import history_for
from selector_drift.validate import quarantine
from selector_drift.validate.quarantine import AlreadyResolved
from tests.orchestrate.conftest import Bench
from tests.orchestrate.test_pipeline_end_to_end import SPEC, rows_in_lake, run, state_of

W0 = dt.date(2026, 9, 1)
W1 = W0 + dt.timedelta(days=1)


def cache_rows() -> int:
    with Session() as s:
        return len(list(s.scalars(select(SelectorCacheEntry))))


def decision(key: str) -> tuple[str | None, str | None]:
    with Session() as s:
        q = s.get(Quarantine, key)
        assert q is not None
        return q.resolved_as, q.actor


@pytest.mark.integration
def test_a_discard_racing_a_promotion_waits_and_then_fails_cleanly(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's probe, made a race between two threads: a discard arriving while the
    promotion is landing must wait for it and then be refused -- never leave the quarantine
    reading "discarded" over rows that landed and a cache that was written."""
    key = run(W0)
    outcome: dict[str, Any] = {}
    real_accept = qa.accept

    def racing(*a: Any, **k: Any) -> Any:
        def discard() -> None:
            try:
                quarantine.discard(key, actor="op2", reason="discarding concurrently")
                outcome["discard"] = "ok"
            except AlreadyResolved:
                outcome["discard"] = "already"

        t = threading.Thread(target=discard)
        t.start()
        time.sleep(1.0)
        outcome["blocked"] = t.is_alive()  # the promotion holds the row
        outcome["thread"] = t
        return real_accept(*a, **k)

    monkeypatch.setattr(qa, "accept", racing)
    qa.build_quarantine_actions().promote(key, "op1", "checked")
    outcome["thread"].join(timeout=30)
    assert outcome["blocked"] is True
    assert outcome["discard"] == "already"
    assert decision(key) == ("promoted", "op1")
    assert rows_in_lake(key) > 0 and cache_rows() == len(SPEC.targets)
    with Session() as s:
        actions = [a.action for a in s.scalars(select(AuditLog).where(AuditLog.subject == key))]
    assert "discard" not in actions and actions.count("promote") == 1


@pytest.mark.integration
def test_a_promotion_racing_a_discard_waits_and_then_lands_nothing(bench: Bench) -> None:
    key = run(W0)
    outcome: dict[str, Any] = {}
    with Session() as a:  # a discard mid-transaction: it holds the row
        q = a.get(Quarantine, key, with_for_update=True)
        assert q is not None and q.resolved_as is None

        def promote() -> None:
            try:
                qa.build_quarantine_actions().promote(key, "op1", "checked")
                outcome["promote"] = "ok"
            except AlreadyResolved:
                outcome["promote"] = "already"

        t = threading.Thread(target=promote)
        t.start()
        time.sleep(1.0)
        assert t.is_alive(), "the promotion did not wait for the discard"
        q.resolved_as, q.actor, q.resolved_at = "discarded", "op2", dt.datetime.now(dt.UTC)
        a.commit()
        t.join(timeout=60)
    assert outcome == {"promote": "already"}
    assert decision(key) == ("discarded", "op2")
    assert rows_in_lake(key) == 0 and cache_rows() == 0


@pytest.mark.integration
def test_a_replay_of_a_discarded_window_stays_discarded(bench: Bench) -> None:
    key = run(W0)
    qa.build_quarantine_actions().discard(key, "op1", "a real portal change")
    run(W0)  # the same window again: held again
    assert decision(key) == ("discarded", "op1")
    with Session() as s:
        q = s.get(Quarantine, key)
        assert q is not None and q.resolved_at is not None
        replays = s.scalars(
            select(AuditLog).where(
                AuditLog.subject == key, AuditLog.action == "quarantine_replayed"
            )
        ).all()
    (replay,) = replays
    assert replay.detail["resolved_as"] == "discarded" and replay.detail["data_uri"] != q.data_uri
    # The read path a human (and the API, Task 34) sees: the replay is listed, never silent.
    (seen,) = quarantine.replays_of(key)
    assert (seen.resolved_as, seen.data_uri) == ("discarded", replay.detail["data_uri"])
    assert quarantine.replays_of("testbench:2:2020-01-01") == []


@pytest.mark.integration
def test_a_replay_of_a_promoted_window_keeps_the_promotion(
    bench: Bench, session: OrmSession
) -> None:
    key = run(W0)
    qa.build_quarantine_actions().promote(key, "op1", "checked")
    landed = rows_in_lake(key)
    with Session() as s:
        q = s.get(Quarantine, key)
        assert q is not None
        promoted_data = q.data_uri
    run(W0)  # replayed: held again on its own data
    assert decision(key) == ("promoted", "op1")
    assert rows_in_lake(key) == landed
    history = history_for(session, "testbench", SPEC)
    assert history is not None and [r.run_key for r in history.runs] == [key]
    with Session() as s:
        q = s.get(Quarantine, key)
        assert q is not None and q.data_uri == promoted_data  # the promoted rows are not lost


@pytest.mark.integration
def test_a_quality_failure_after_landing_never_marks_the_run_failed(
    bench: Bench, session: OrmSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    key0 = run(W0)
    qa.build_quarantine_actions().promote(key0, "op1", "checked")

    def broken(key: str) -> None:
        raise RuntimeError("duckdb went away")

    monkeypatch.setattr("selector_drift.orchestrate.pipeline.evaluate", broken)
    key = run(W1)  # rows land and the run is accepted; then quality breaks
    assert state_of(key) == "completed" and rows_in_lake(key) > 0
    run_ = session.get(Run, key)
    assert run_ is not None and (run_.error or "").startswith("quality not evaluated")
    history = history_for(session, "testbench", SPEC, before=W1 + dt.timedelta(days=1))
    assert history is not None and key in [r.run_key for r in history.runs]


@pytest.mark.unit
def test_a_quality_failure_is_logged_with_its_traceback_but_not_its_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fix round 2: the run note and the alert are not enough to debug from; the traceback is
    logged too -- frames and the exception TYPE only, never its message (it may carry page text)."""
    from selector_drift.orchestrate import pipeline

    secret = "claim AB12345678 for Jane Doe"

    def broken(key: str) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(pipeline, "evaluate", broken)
    monkeypatch.setattr(pipeline, "post_alert", lambda *a, **k: None)
    with caplog.at_level("ERROR", logger=pipeline.__name__):
        findings, note = pipeline._evaluate("testbench:2:2026-09-01", "testbench")
    assert findings == [] and note is not None and note.startswith("quality not evaluated")
    (record,) = [r for r in caplog.records if r.name == pipeline.__name__]
    text = record.getMessage()
    assert "RuntimeError" in text and "broken" in text  # the type and the failing frame
    assert secret not in text and record.exc_info is None
