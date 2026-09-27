from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from selector_drift.db.models import AuditLog, Heartbeat, Run, RunAttempt, ScopeRecordRow
from selector_drift.db.session import Session
from selector_drift.orchestrate.flows import extraction_flow
from selector_drift.orchestrate.run_record import (
    NO_TERMINAL_STATE,
    heartbeat,
    record_attempt,
    run_key,
)
from selector_drift.spec.loader import load_spec
from selector_drift.spec.schema import SpecError

REPO = Path(__file__).resolve().parents[2]
# A failing test must not sleep through two 300 s retries. The retry policy itself is pinned
# in test_deployments.py.
ONCE = extraction_flow.with_options(retries=0)


def _today() -> str:
    return dt.datetime.now(dt.UTC).date().isoformat()


def _bench_key() -> str:
    return run_key("testbench", load_spec(REPO / "specs" / "testbench.yaml").version, _today())


@pytest.mark.integration
def test_flow_writes_a_run_record(prefect_api, db, local_site) -> None:
    """Tightened from the first draft, which accepted `failed` as a pass."""
    key = ONCE("specs/testbench.yaml")
    with Session() as s:
        assert s.get(Run, key).state == "completed"
        (attempt,) = s.query(RunAttempt).filter_by(run_key=key).all()
        assert (attempt.attempt, attempt.state, attempt.error) == (1, "completed", None)
        assert s.query(Heartbeat).filter_by(run_key=key).count() == 1


@pytest.mark.unit
def test_run_key_is_deterministic() -> None:
    assert run_key("t", 1, "2026-09-07") == run_key("t", 1, "2026-09-07")


@pytest.mark.integration
def test_a_refused_target_never_opens_a_browser(prefect_api, db, expired_spec, monkeypatch) -> None:
    opened: list[int] = []
    monkeypatch.setattr("selector_drift.browser.driver.open", lambda *a, **k: opened.append(1))
    key = ONCE(expired_spec)
    with Session() as s:
        run = s.get(Run, key)
        assert run.state == "refused" and "expired" in (run.error or "")
        # Task 34B's fired_without_attempt depends on this absence.
        assert s.query(RunAttempt).filter_by(run_key=key).count() == 0
    assert opened == []


@pytest.mark.integration
def test_every_fire_writes_a_heartbeat_even_when_refused(prefect_api, db, expired_spec) -> None:
    key = ONCE(expired_spec)
    with Session() as s:
        hb = s.query(Heartbeat).filter_by(run_key=key).one()
    assert hb.target == "expired"
    assert hb.fired_at >= hb.scheduled_for.replace(second=0, microsecond=0)


@pytest.mark.integration
def test_the_heartbeat_is_written_before_authorisation(
    prefect_api, db, local_site, monkeypatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("scope gate unreachable")

    monkeypatch.setattr("selector_drift.orchestrate.flows.authorise", boom)
    with pytest.raises(RuntimeError, match="unreachable"):
        ONCE("specs/testbench.yaml")
    with Session() as s:
        hb = s.query(Heartbeat).filter_by(target="testbench").one()
        assert hb.run_key == _bench_key()
        assert s.get(Run, _bench_key()) is None  # no runs row, no attempt row


@pytest.mark.integration
def test_an_unloadable_spec_still_leaves_a_heartbeat(prefect_api, db, tmp_path) -> None:
    bad = tmp_path / "ghost.yaml"
    bad.write_text("version: [not, an, int]\n", encoding="utf-8")
    with pytest.raises(SpecError):
        ONCE(str(bad))
    with Session() as s:
        hb = s.query(Heartbeat).filter_by(target="ghost").one()
    assert hb.run_key is None


@pytest.mark.integration
def test_an_unknown_target_is_refused_not_failed(prefect_api, db, tmp_path, scope_dir) -> None:
    """Design 4.3: no scope record -> Refuse, with a runs row the log can count."""
    specs = tmp_path / "specs"
    specs.mkdir()
    body = (REPO / "specs" / "testbench.yaml").read_text(encoding="utf-8")
    (specs / "nowhere.yaml").write_text(
        body.replace("scopes/testbench.yaml", "scopes/nowhere.yaml"), encoding="utf-8"
    )
    key = ONCE(str(specs / "nowhere.yaml"))
    with Session() as s:
        run = s.get(Run, key)
        assert run.state == "refused" and "unknown target" in (run.error or "")


@pytest.mark.integration
def test_a_page_that_will_not_load_is_a_failed_attempt_and_the_flow_raises(
    prefect_api, db, dead_site
) -> None:
    """Design 9.3 Fail: marked failed, alerted, and raised so Prefect's retry policy applies."""
    # The exception type is Task 4's; any failure that carries a reason will do.
    with pytest.raises(Exception, match=r"\S"):
        ONCE("specs/testbench.yaml")
    with Session() as s:
        run = s.get(Run, _bench_key())
        assert run.state == "failed" and run.error and run.error != NO_TERMINAL_STATE
        (attempt,) = s.query(RunAttempt).filter_by(run_key=_bench_key()).all()
        assert attempt.state == "failed" and attempt.finished_at is not None


@pytest.mark.integration
def test_a_second_run_in_the_same_window_is_a_second_attempt_not_an_overwrite(
    prefect_api, db, local_site
) -> None:
    """M13. A retry must not erase the attempt it retried."""
    first = ONCE("specs/testbench.yaml")
    second = ONCE("specs/testbench.yaml")
    assert first == second
    with Session() as s:
        attempts = s.query(RunAttempt).filter_by(run_key=first).order_by(RunAttempt.attempt).all()
    assert [(a.attempt, a.state) for a in attempts] == [(1, "completed"), (2, "completed")]


@pytest.mark.integration
def test_a_crash_after_start_leaves_a_failed_run_not_a_missing_one(db) -> None:
    key = "testbench:1:2026-09-07"
    record_attempt(key, 1, "started", None, "0" * 64)
    with Session() as s:
        run = s.get(Run, key)
        assert (run.state, run.error) == ("failed", NO_TERMINAL_STATE)
        assert run.window == dt.date(2026, 9, 7) and run.spec_version == 1
        assert s.query(RunAttempt).filter_by(run_key=key).one().finished_at is None


@pytest.mark.integration
def test_a_retry_of_the_same_slot_does_not_double_count_its_heartbeat(db) -> None:
    slot = dt.datetime(2026, 9, 7, 4, 12, tzinfo=dt.UTC)
    first = heartbeat("testbench", slot, None)
    assert heartbeat("testbench", slot, None) == first
    with Session() as s:
        assert s.query(Heartbeat).count() == 1


@pytest.mark.integration
def test_every_authorised_run_audits_the_scope_record_hash(prefect_api, db, local_site) -> None:
    """Task 2: which authorisation was in force is answerable from the database."""
    key = ONCE("specs/testbench.yaml")
    with Session() as s:
        row = s.query(AuditLog).filter_by(action="scope_authorised", subject=key).one()
        attempt = s.query(RunAttempt).filter_by(run_key=key).one()
    assert row.detail["record_sha256"] == attempt.scope_record_sha256
    assert row.detail["target"] == "testbench"


@pytest.mark.integration
def test_every_authorised_run_mirrors_its_scope_record(prefect_api, db, local_site) -> None:
    """Found 2026-09-26: nothing wrote scope_records, which the targets screen and the retention
    job read. The mirror carries the hash the audit row carries."""
    key = ONCE("specs/testbench.yaml")
    with Session() as s:
        mirror = s.get(ScopeRecordRow, "testbench")
        attempt = s.query(RunAttempt).filter_by(run_key=key).one()
    assert mirror is not None and mirror.record_sha256 == attempt.scope_record_sha256
    assert mirror.warmup_runs >= 1 and mirror.loaded_at is not None
    assert mirror.credential_posture == "session_injection"
