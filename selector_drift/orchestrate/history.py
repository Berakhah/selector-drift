"""Builds validate.History from what was WRITTEN. validate/ never reads storage itself.

The ONE builder (ledger 583): the gate's history (pipeline.run_once, quarantine_actions'
rebuild) and the quality screen's distribution reference (quality.contracts) both come from
`history_for`, so a heal-log line and a quality row can never be judged against different pasts.

A run is history when its rows were accepted -- the gate passed it (`completed` / `healed`) or a
human promoted it (its quarantine row says `promoted`) -- under the CURRENT spec version (ledger
471: History carries no version, and a column under another version meant something else), in a
window before the one being judged (a retry of an accepted window must not be compared with its
own rows). Rows are read back through the lake view, which deduplicates and survives compaction.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from selector_drift.db.models import AuditLog, FindingRow, Quarantine, Run
from selector_drift.land.catalog import query
from selector_drift.land.layout import NATURAL_KEY_COL, RUN_KEY_COL, is_data_column
from selector_drift.spec.schema import TaskSpec
from selector_drift.validate.history import AcceptedRun, History

ACCEPTED_STATES = ("completed", "healed")
DEFAULT_LIMIT = 90


def _lake_rows(target: str, run_keys: list[str]) -> dict[str, list[dict[str, str]]]:
    """Each run's landed rows in natural-key order, data columns only, values as written.
    `select *` only: on a lake with no files the view has no data columns, and naming one is a
    binder error (ledger 569)."""
    found = query(
        f"select * from lake where list_contains(?, {RUN_KEY_COL}) order by {NATURAL_KEY_COL}",
        [run_keys],
        target=target,
    ).dicts()
    out: dict[str, list[dict[str, str]]] = {}
    for r in found:
        out.setdefault(str(r[RUN_KEY_COL]), []).append(_data(r))
    return out


def _data(row: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in row.items() if is_data_column(k) and v is not None}


def accepted_run() -> ColumnElement[bool]:
    """THE predicate for "this run's rows were accepted", over `Run`: the gate passed it, or a
    human promoted its quarantine. One definition, so the bench scorer (Task 26C) judges
    acceptance exactly as history does."""
    promoted = select(Quarantine.run_key).where(Quarantine.resolved_as == "promoted")
    return Run.state.in_(ACCEPTED_STATES) | Run.run_key.in_(promoted)


def history_for(
    db: Session,
    target: str,
    spec: TaskSpec,
    *,
    before: dt.date | None = None,
    limit: int = DEFAULT_LIMIT,
) -> History | None:
    """The newest `limit` accepted runs of `target` under `spec.version` with a window before
    `before`, oldest first. None when there are none (no history is not an empty history)."""
    stmt = select(Run).where(
        Run.target == target,
        Run.spec_version == spec.version,
        accepted_run(),
    )
    if before is not None:
        stmt = stmt.where(Run.window < before)
    runs = list(db.scalars(stmt.order_by(Run.window.desc()).limit(limit)).all())
    if not runs:
        return None
    keys = [r.run_key for r in runs]
    # shift_alerts are BREACHES, not alerts: quality writes a distribution_shift finding on every
    # breach, so DistributionalAssertion can count persistence (ledger 2026-09-26).
    breached: dict[str, set[str]] = {}
    for key, payload in db.execute(
        select(FindingRow.run_key, FindingRow.payload).where(
            FindingRow.run_key.in_(keys), FindingRow.rule == "distribution_shift"
        )
    ):
        field = (payload or {}).get("properties", {}).get("field")
        if isinstance(field, str):
            breached.setdefault(key, set()).add(field)
    marked: dict[str, set[str]] = {}
    for key, detail in db.execute(
        select(AuditLog.subject, AuditLog.detail).where(
            AuditLog.action == "marked_expected", AuditLog.subject.in_(keys)
        )
    ):
        field = (detail or {}).get("field")
        if isinstance(field, str):
            marked.setdefault(key, set()).add(field)
    landed = _lake_rows(target, keys)
    return History(
        runs=tuple(
            AcceptedRun(
                run_key=run.run_key,
                window=run.window,
                rows=tuple(landed.get(run.run_key, ())),
                natural_key=tuple(spec.natural_key),
                shift_alerts=frozenset(breached.get(run.run_key, ())),
                marked_expected=frozenset(marked.get(run.run_key, ())),
            )
            for run in reversed(runs)
        )
    )
