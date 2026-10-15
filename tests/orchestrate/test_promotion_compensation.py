"""Task 26A fix round 2: a promotion that fails part-way leaves NOTHING behind. The cache and the
lake are written through their own sessions, so a database rollback does not undo them; accept()
hands back its own compensation and promote_and_land runs it on any failure, leaving the
quarantine open and a re-promotion clean."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from selector_drift.config import get_settings
from selector_drift.db.models import AuditLog, Quarantine, SelectorCacheEntry
from selector_drift.db.session import Session
from selector_drift.orchestrate import quarantine_actions as qa
from selector_drift.orchestrate.cache_writer import CacheWriter
from selector_drift.validate import quarantine
from tests.orchestrate.conftest import Bench
from tests.orchestrate.test_pipeline_end_to_end import SPEC, rows_in_lake, run

W0 = dt.date(2026, 9, 1)


def landed_files() -> list[Path]:
    return sorted(get_settings().lake_root.glob("target=*/window=*/*.parquet"))


def cache_rows() -> list[SelectorCacheEntry]:
    with Session() as s:
        return list(s.scalars(select(SelectorCacheEntry)))


def still_open(key: str) -> bool:
    with Session() as s:
        q = s.get(Quarantine, key)
        return q is not None and q.resolved_as is None


def audit_actions(key: str) -> list[str]:
    with Session() as s:
        return [a.action for a in s.scalars(select(AuditLog).where(AuditLog.subject == key))]


def assert_nothing_left_behind(key: str) -> None:
    assert landed_files() == [] and rows_in_lake(key) == 0
    assert cache_rows() == []
    assert still_open(key)
    assert "promote" not in audit_actions(key)


def promote_cleanly(key: str) -> None:
    qa.build_quarantine_actions().promote(key, "op1", "checked again")
    assert rows_in_lake(key) > 0 and len(cache_rows()) == len(SPEC.targets)
    assert len(landed_files()) == 1
    assert audit_actions(key).count("promote") == 1


def fail_second_put(monkeypatch: pytest.MonkeyPatch) -> None:
    real, calls = CacheWriter.put, {"n": 0}

    def put(self: CacheWriter, *a: Any, **k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # the first field is already in the cache
            raise OSError("cache went away")
        return real(self, *a, **k)

    monkeypatch.setattr(CacheWriter, "put", put)


def fail_last_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise OSError("evidence disk full")

    monkeypatch.setattr(qa, "promote_heal_bundle_to_last_accepted", boom)


def fail_final_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    real = quarantine._resolve_promoted

    def staged_then_boom(*a: Any, **k: Any) -> None:
        real(*a, **k)
        raise OSError("connection lost at commit")

    monkeypatch.setattr(quarantine, "_resolve_promoted", staged_then_boom)


FAILURES = [fail_second_put, fail_last_accepted, fail_final_mark]
IDS = ["cache_put", "commit_last_accepted", "final_mark"]


@pytest.mark.integration
@pytest.mark.parametrize("inject", FAILURES, ids=IDS)
def test_a_failed_promotion_leaves_nothing_and_a_repromotion_lands_once(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, inject: Any
) -> None:
    key = run(W0)
    with monkeypatch.context() as m:
        inject(m)
        with pytest.raises(OSError):
            qa.build_quarantine_actions().promote(key, "op1", "checked")
    assert_nothing_left_behind(key)
    promote_cleanly(key)


@pytest.mark.integration
@pytest.mark.parametrize("inject", FAILURES, ids=IDS)
def test_a_discard_after_a_failed_promotion_agrees_with_the_empty_lake(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, inject: Any
) -> None:
    key = run(W0)
    with monkeypatch.context() as m:
        inject(m)
        with pytest.raises(OSError):
            qa.build_quarantine_actions().promote(key, "op1", "checked")
    qa.build_quarantine_actions().discard(key, "op2", "not trusting it")
    assert landed_files() == [] and rows_in_lake(key) == 0 and cache_rows() == []
    actions = audit_actions(key)
    assert "discard" in actions and "promote" not in actions


@pytest.mark.integration
def test_compaction_is_deferred_past_a_failed_final_mark(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 3: with compaction due on every write, a promotion whose final mark fails must
    still find its landed file to remove -- compaction (which cannot be undone) has not run."""
    from selector_drift.land import writer as land_writer

    key = run(W0)
    monkeypatch.setattr(land_writer, "COMPACT_AFTER_FILES", 0)
    compactions: list[str] = []
    real = land_writer.compact_if_needed

    def spy(written: Path) -> None:
        compactions.append(str(written))
        real(written)  # the real thing: a premature compaction would really fold the file

    monkeypatch.setattr(land_writer, "compact_if_needed", spy)
    with monkeypatch.context() as m:
        fail_final_mark(m)
        with pytest.raises(OSError):
            qa.build_quarantine_actions().promote(key, "op1", "checked")
    assert_nothing_left_behind(key)
    assert compactions == []  # never ran for a promotion that was taken back


@pytest.mark.integration
def test_compaction_runs_only_after_the_mark_commits(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    from selector_drift.land import writer as land_writer

    key = run(W0)
    monkeypatch.setattr(land_writer, "COMPACT_AFTER_FILES", 0)
    real = land_writer.compact_if_needed
    seen: list[bool] = []

    def compact(written: Path) -> None:
        seen.append(not still_open(key))  # was the mark already committed when we ran?
        real(written)

    monkeypatch.setattr(land_writer, "compact_if_needed", compact)
    qa.build_quarantine_actions().promote(key, "op1", "checked")
    assert seen == [True]  # ran exactly once, after the commit
    assert audit_actions(key).count("promote") == 1 and rows_in_lake(key) > 0
