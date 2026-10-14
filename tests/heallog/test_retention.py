from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from selector_drift.config import get_settings
from selector_drift.db.models import AuditLog, HealEvent, Run, ScopeRecordRow
from selector_drift.db.session import Session, engine
from selector_drift.heallog.retention import evidence_retention
from selector_drift.orchestrate.deployments import retention_deployment

# `.fn` calls the flow body directly: these tests exercise retention, not Prefect.

TODAY = dt.date(2026, 12, 31)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("SD_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    get_settings.cache_clear()
    with engine.begin() as c:
        c.execute(
            text("TRUNCATE runs, heal_events, scope_records, audit_log RESTART IDENTITY CASCADE")
        )
    yield tmp_path / "evidence"
    get_settings.cache_clear()


def _scope(target: str, basis: str, days: int | None) -> ScopeRecordRow:
    return ScopeRecordRow(
        target=target,
        basis=basis,
        authorising_party="me",
        authorised_on=dt.date(2026, 1, 1),
        expires_on=dt.date(2027, 1, 1),
        evidence_uri=None,
        evidence_sha256=None,
        origins=["https://x.example.invalid"],
        rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
        warmup_runs=30,
        evidence_retention_days=days,
        record_sha256="0" * 64,
        loaded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _heal(root: Path, target: str, broke: dt.date) -> int:
    key = f"{target}:1:{broke.isoformat()}"
    with Session() as s:
        s.add(
            Run(
                run_key=key,
                target=target,
                spec_version=1,
                window=broke,
                first_started_at=datetime.combine(broke, dt.time(4, 12), UTC),
                state="healed",
            )
        )
        s.flush()  # the run row first: heal_events.run_key references it
        heal = HealEvent(
            run_key=key,
            target=target,
            field="claim_number",
            broke_at=datetime.combine(broke, dt.time(4, 12), UTC),
            winning_rung=3,
            selector_after="td",
            anchor_after={},
            trace=[],
            verdicts=[],
        )
        s.add(heal)
        s.commit()
        bundle = root / "heals" / str(heal.id)
        bundle.mkdir(parents=True)
        (bundle / "after.html").write_text("<html></html>")
        heal.evidence_uri = str(bundle)
        s.commit()
        return heal.id


@pytest.mark.integration
def test_retention_deletes_client_bundles_and_keeps_counts(db: Path) -> None:
    """API spec 7.4 / 7.5, closes M30."""
    with Session() as s:
        s.add(_scope("client-portal", "client-authorised", 90))
        s.commit()
    old = _heal(db, "client-portal", TODAY - dt.timedelta(days=91))
    young = _heal(db, "client-portal", TODAY - dt.timedelta(days=10))
    assert evidence_retention.fn(today=TODAY) == 1
    assert not (db / "heals" / str(old)).exists()
    assert (db / "heals" / str(young)).exists()
    with Session() as s:
        assert s.query(HealEvent).count() == 2  # the counts remain
        row = s.query(AuditLog).filter_by(action="evidence_expired").one()
        assert row.subject == f"heal:{old}" and row.actor == "system:evidence_retention"
        assert row.detail["retention_days"] == 90


@pytest.mark.integration
def test_self_owned_bundles_are_kept_for_the_life_of_the_log(db: Path) -> None:
    with Session() as s:
        s.add(_scope("testbench", "self-owned", None))
        s.commit()
    kept = _heal(db, "testbench", TODAY - dt.timedelta(days=400))
    assert evidence_retention.fn(today=TODAY) == 0
    assert (db / "heals" / str(kept)).exists()


@pytest.mark.integration
def test_a_heal_with_no_scope_record_is_kept_and_flagged(db: Path) -> None:
    orphan = _heal(db, "ghost", TODAY - dt.timedelta(days=400))
    assert evidence_retention.fn(today=TODAY) == 0
    assert (db / "heals" / str(orphan)).exists()
    with Session() as s:
        assert s.query(AuditLog).filter_by(action="retention_skipped").count() == 1


@pytest.mark.integration
def test_retention_deletes_by_heal_id_never_by_the_stored_uri(db: Path, tmp_path: Path) -> None:
    """S-M4: a tampered evidence_uri cannot point the deletion anywhere else."""
    with Session() as s:
        s.add(_scope("client-portal", "client-authorised", 90))
        s.commit()
    heal_id = _heal(db, "client-portal", TODAY - dt.timedelta(days=91))
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("not evidence")
    with Session() as s:
        heal = s.get(HealEvent, heal_id)
        assert heal is not None
        heal.evidence_uri = str(victim)
        s.commit()
    assert evidence_retention.fn(today=TODAY) == 1
    assert (victim / "keep.txt").exists()
    assert not (db / "heals" / str(heal_id)).exists()


@pytest.mark.unit
def test_retention_is_a_daily_deployment() -> None:
    dep = retention_deployment()
    assert dep.name == "evidence-retention"
    assert dep.schedules[0].schedule.cron == "30 3 * * *"


@pytest.mark.unit
def test_methodology_states_the_retention_policy() -> None:
    text_ = Path("docs/methodology.md").read_text(encoding="utf-8").lower()
    assert "## retention" in text_ and "disk budget" in text_
    assert "evidence_retention_days" in text_
