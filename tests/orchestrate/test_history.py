"""orchestrate.history.history_for: the ONE builder of validate.History (Task 26A).

Built from what was WRITTEN -- runs the gate accepted or a human promoted, under the current spec
version, before the window being judged -- with rows read back through the lake view.
quality.contracts reads its distribution reference through the same function (ledger 583)."""

from __future__ import annotations

import ast
import datetime as dt
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from selector_drift.config import get_settings
from selector_drift.spec.loader import load_spec_text
from tests.land.factory import SPEC_YAML, configure_lake, make_validated, rows

SPEC = load_spec_text(SPEC_YAML)  # version 2: claim_number + amount
BASE = dt.date(2026, 9, 1)
Seed = Callable[..., str]


@pytest.fixture
def seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Seed]:
    from selector_drift.db.models import Quarantine, Run
    from selector_drift.db.session import Session, engine
    from selector_drift.land.writer import write

    configure_lake(tmp_path, monkeypatch)
    with engine.begin() as c:
        c.execute(text("TRUNCATE runs, findings, audit_log, quarantine RESTART IDENTITY CASCADE"))

    def _seed(
        day: dt.date,
        state: str = "completed",
        *,
        version: int = 2,
        land: bool = True,
        resolved_as: str | None = None,
        n: int = 12,
    ) -> str:
        key = f"testbench:{version}:{day.isoformat()}"
        with Session() as s:
            s.merge(
                Run(
                    run_key=key,
                    target="testbench",
                    spec_version=version,
                    window=day,
                    first_started_at=dt.datetime.combine(day, dt.time(4, 12), dt.UTC),
                    state=state,
                )
            )
            if state == "quarantined":
                s.merge(
                    Quarantine(
                        run_key=key,
                        reason="test",
                        verdicts=[],
                        data_uri="",
                        resolved_as=resolved_as,
                    )
                )
            s.commit()
        if land:
            write(make_validated(rows(n), key))
        return key

    yield _seed
    get_settings.cache_clear()


def _history(**kw: object):  # type: ignore[no-untyped-def]
    from selector_drift.db.session import Session
    from selector_drift.orchestrate.history import history_for

    with Session() as db:
        return history_for(db, "testbench", SPEC, **kw)  # type: ignore[arg-type]


@pytest.mark.integration
def test_no_accepted_run_is_no_history(seed: Seed) -> None:
    seed(BASE, "quarantined", land=False)
    seed(BASE + dt.timedelta(days=1), "failed", land=False)
    assert _history() is None


@pytest.mark.integration
def test_only_accepted_and_promoted_runs_are_history(seed: Seed) -> None:
    accepted = seed(BASE)
    healed = seed(BASE + dt.timedelta(days=1), "healed")
    seed(BASE + dt.timedelta(days=2), "quarantined", land=False)  # held, never landed
    seed(BASE + dt.timedelta(days=3), "quarantined", land=False, resolved_as="discarded")
    promoted = seed(BASE + dt.timedelta(days=4), "quarantined", resolved_as="promoted")
    seed(BASE + dt.timedelta(days=5), "failed", land=False)
    seed(BASE + dt.timedelta(days=6), "refused", land=False)
    history = _history()
    assert [r.run_key for r in history.runs] == [accepted, healed, promoted]  # oldest first


@pytest.mark.integration
def test_history_is_filtered_to_the_current_spec_version(seed: Seed) -> None:
    """Ledger 471: History carries no spec version, so the reference window must be filtered
    here -- a v1 run's columns meant something else."""
    seed(BASE, version=1, land=False)
    current = seed(BASE + dt.timedelta(days=1))
    assert [r.run_key for r in _history().runs] == [current]


@pytest.mark.integration
def test_history_holds_only_windows_before_the_one_being_judged(seed: Seed) -> None:
    """A retry of an already-accepted window must not compare the run with its own rows."""
    earlier = seed(BASE)
    seed(BASE + dt.timedelta(days=1))
    assert [r.run_key for r in _history(before=BASE + dt.timedelta(days=1)).runs] == [earlier]


@pytest.mark.integration
def test_history_keeps_the_newest_runs_up_to_the_limit(seed: Seed) -> None:
    keys = [seed(BASE + dt.timedelta(days=i)) for i in range(4)]
    assert [r.run_key for r in _history(limit=2).runs] == keys[2:]


@pytest.mark.integration
def test_rows_are_read_back_through_the_lake_as_data_columns(seed: Seed) -> None:
    key = seed(BASE, n=3)
    (run,) = _history().runs
    assert run.run_key == key and run.window == BASE
    assert run.natural_key == ("claim_number",)
    assert run.rows == tuple(
        {"claim_number": r["claim_number"], "amount": r["amount"]} for r in rows(3)
    )


@pytest.mark.integration
def test_an_accepted_run_with_nothing_in_the_lake_has_no_rows_not_an_error(seed: Seed) -> None:
    """Ledger 569: on an empty lake `select amount from lake` is a binder error."""
    key = seed(BASE, land=False)
    (run,) = _history().runs
    assert (run.run_key, run.rows) == (key, ())


@pytest.mark.integration
def test_breaches_and_client_marks_come_from_findings_and_the_audit_log(seed: Seed) -> None:
    from selector_drift.db.models import AuditLog, FindingRow
    from selector_drift.db.session import Session

    key = seed(BASE)
    now = dt.datetime.now(dt.UTC)
    with Session() as s:
        s.add(
            FindingRow(
                id=f"quality/distribution_shift/amount@{key}",
                run_key=key,
                severity="MEDIUM",
                rule="distribution_shift",
                payload={"properties": {"field": "amount"}},
                at=now,
            )
        )
        s.add(
            AuditLog(
                at=now,
                actor="op1",
                action="marked_expected",
                subject=key,
                detail={"field": "amount"},
            )
        )
        s.commit()
    (run,) = _history().runs
    assert run.shift_alerts == frozenset({"amount"})
    assert run.marked_expected == frozenset({"amount"})


@pytest.mark.unit
def test_quality_contracts_builds_no_history_of_its_own() -> None:
    """Ledger 583: one builder, never two that can disagree."""
    src = Path("selector_drift/quality/contracts.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    builds = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "AcceptedRun"
    ]
    assert builds == []
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "history_for" in calls
