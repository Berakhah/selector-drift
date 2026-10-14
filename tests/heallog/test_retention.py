from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from selector_drift.config import get_settings
from selector_drift.db.models import AuditLog, HealEvent, Quarantine, Run, ScopeRecordRow
from selector_drift.db.session import Session, engine
from selector_drift.heallog import retention as retention_mod
from selector_drift.heallog.evidence import last_accepted_dir
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
            text(
                "TRUNCATE runs, heal_events, scope_records, audit_log, quarantine "
                "RESTART IDENTITY CASCADE"
            )
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


# -- fix round 1 -------------------------------------------------------------------------------


def _age_out(path: Path, days: int = 3) -> None:
    then = (datetime.now(UTC) - dt.timedelta(days=days)).timestamp()
    os.utime(path, (then, then))


def _client(days: int = 90, expires: dt.date = dt.date(2027, 1, 1)) -> None:
    with Session() as s:
        row = _scope("client-portal", "client-authorised", days)
        row.expires_on = expires
        s.add(row)
        s.commit()


@pytest.mark.integration
def test_a_heal_with_an_open_quarantine_is_kept(db: Path) -> None:
    """I2: promotion needs the bundle; retention waits until the quarantine is resolved."""
    _client()
    heal_id = _heal(db, "client-portal", TODAY - dt.timedelta(days=200))
    with Session() as s:
        heal = s.get(HealEvent, heal_id)
        assert heal is not None
        s.add(Quarantine(run_key=heal.run_key, reason="first heal", verdicts=[], data_uri="x"))
        s.commit()
    assert evidence_retention.fn(today=TODAY) == 0
    assert (db / "heals" / str(heal_id)).exists()
    with Session() as s:
        row = s.query(AuditLog).filter_by(action="retention_skipped").one()
        assert row.detail["why"] == "quarantine open"
        q = s.query(Quarantine).one()
        q.resolved_as = "discarded"
        s.commit()
    assert evidence_retention.fn(today=TODAY) == 1
    assert not (db / "heals" / str(heal_id)).exists()


@pytest.mark.integration
def test_every_audit_row_is_committed_before_its_delete(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I4: a crash during the delete leaves the evidence_expired row, never a silent gap."""
    _client()
    heal_id = _heal(db, "client-portal", TODAY - dt.timedelta(days=91))

    def crash(path: object) -> None:
        raise RuntimeError("host died mid-delete")

    monkeypatch.setattr(retention_mod.shutil, "rmtree", crash)
    with pytest.raises(RuntimeError):
        evidence_retention.fn(today=TODAY)
    with Session() as s:
        row = s.query(AuditLog).filter_by(action="evidence_expired").one()
        assert row.subject == f"heal:{heal_id}"


@pytest.mark.integration
def test_a_bundle_whose_uri_was_never_persisted_is_still_retained(db: Path) -> None:
    _client()
    heal_id = _heal(db, "client-portal", TODAY - dt.timedelta(days=91))
    with Session() as s:
        heal = s.get(HealEvent, heal_id)
        assert heal is not None
        heal.evidence_uri = None
        s.commit()
    assert evidence_retention.fn(today=TODAY) == 1
    assert not (db / "heals" / str(heal_id)).exists()


@pytest.mark.integration
def test_unreachable_directories_are_swept_and_audited(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I3: a heals/<n> with no heal row, crash leftovers, and a last-accepted directory no
    live scope record names."""
    monkeypatch.setenv("SD_REDACTION_KEY", ("k" * 32).encode().hex())
    get_settings.cache_clear()
    _client(expires=dt.date(2026, 6, 30))  # expired before TODAY
    orphan = db / "heals" / "999"
    orphan.mkdir(parents=True)
    (orphan / "after.html").write_text("<html></html>")
    leftover = db / "heals" / ".tmp-0123abcd"
    leftover.mkdir()
    young = db / "heals" / ".tmp-fresh"
    young.mkdir()
    accepted = last_accepted_dir("client-portal")
    accepted.mkdir(parents=True)
    (accepted / "page.html").write_text("<html></html>")
    for path in (orphan, leftover, accepted):
        _age_out(path)
    assert evidence_retention.fn(today=TODAY) == 3
    assert not orphan.exists() and not leftover.exists() and not accepted.exists()
    assert young.exists()  # a write in progress is never swept
    with Session() as s:
        actions = sorted(r.action for r in s.query(AuditLog).all())
        subjects = " ".join(r.subject for r in s.query(AuditLog).all())
    assert actions == [
        "evidence_leftover_removed",
        "evidence_orphan_removed",
        "last_accepted_removed",
    ]
    assert "client-portal" not in subjects


@pytest.mark.integration
def test_a_live_targets_last_accepted_is_kept(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SD_REDACTION_KEY", ("k" * 32).encode().hex())
    get_settings.cache_clear()
    _client()
    accepted = last_accepted_dir("client-portal")
    accepted.mkdir(parents=True)
    _age_out(accepted)
    assert evidence_retention.fn(today=TODAY) == 0
    assert accepted.exists()
