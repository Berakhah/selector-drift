"""Fix-wave item 2 (Important 4): an exact snapshot of the ORM metadata's tables and columns
(name, type, nullability), so any schema change -- an added column, a widened type, a dropped
NOT NULL -- shows as a visible diff in this file rather than silently passing every other test
in `tests/db/test_schema.py` (which only asserts subsets). Pure metadata introspection, no
database needed.

`test_alembic_has_no_undetected_drift` is the other half: the snapshot only proves the models
are what we think they are, not that the migration files actually produce that schema. It shells
out to `alembic check` against the already-migrated `_test` database (module docstring of
`tests/conftest.py` explains why that database, never any other, is safe to touch).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import pytest

from selector_drift.db.models import Base

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _ColumnSnapshot(TypedDict):
    type: str
    nullable: bool


# name -> {column_name: {"type": SQLAlchemy type class name, "nullable": bool}}
EXPECTED_COLUMNS: dict[str, dict[str, _ColumnSnapshot]] = {
    "audit_log": {
        "id": {"type": "Integer", "nullable": False},
        "at": {"type": "DateTime", "nullable": False},
        "actor": {"type": "Text", "nullable": False},
        "action": {"type": "Text", "nullable": False},
        "subject": {"type": "Text", "nullable": False},
        "detail": {"type": "JSONB", "nullable": False},
    },
    "deploys": {
        "target": {"type": "Text", "nullable": False},
        "deployed_at": {"type": "DateTime", "nullable": False},
        "ref": {"type": "Text", "nullable": False},
        "source": {"type": "Text", "nullable": False},
        "ingested_at": {"type": "DateTime", "nullable": False},
    },
    "findings": {
        "id": {"type": "Text", "nullable": False},
        "run_key": {"type": "Text", "nullable": False},
        "severity": {"type": "Text", "nullable": False},
        "rule": {"type": "Text", "nullable": False},
        "payload": {"type": "JSONB", "nullable": False},
        "at": {"type": "DateTime", "nullable": False},
    },
    "heal_events": {
        "id": {"type": "Integer", "nullable": False},
        "run_key": {"type": "Text", "nullable": False},
        "resolution_id": {"type": "Integer", "nullable": True},
        "target": {"type": "Text", "nullable": False},
        "field": {"type": "Text", "nullable": False},
        "broke_at": {"type": "DateTime", "nullable": False},
        "winning_rung": {"type": "Integer", "nullable": False},
        "confirmer_rung": {"type": "Integer", "nullable": True},
        "confirmer_family": {"type": "Text", "nullable": True},
        "selector_before": {"type": "Text", "nullable": True},
        "selector_after": {"type": "Text", "nullable": False},
        "anchor_before": {"type": "JSONB", "nullable": True},
        "anchor_after": {"type": "JSONB", "nullable": False},
        "trace": {"type": "JSONB", "nullable": False},
        "verdicts": {"type": "JSONB", "nullable": False},
        "evidence_uri": {"type": "Text", "nullable": True},
        "replayed_ok": {"type": "Boolean", "nullable": True},
        "promoted_by": {"type": "Text", "nullable": True},
        "promoted_at": {"type": "DateTime", "nullable": True},
    },
    "heartbeats": {
        "id": {"type": "Integer", "nullable": False},
        "target": {"type": "Text", "nullable": False},
        "scheduled_for": {"type": "DateTime", "nullable": False},
        "fired_at": {"type": "DateTime", "nullable": False},
        "run_key": {"type": "Text", "nullable": True},
    },
    "interventions": {
        "id": {"type": "Integer", "nullable": False},
        "at": {"type": "DateTime", "nullable": False},
        "actor": {"type": "Text", "nullable": False},
        "kind": {"type": "Text", "nullable": False},
        "target": {"type": "Text", "nullable": False},
        "field": {"type": "Text", "nullable": True},
        "before": {"type": "JSONB", "nullable": True},
        "after": {"type": "JSONB", "nullable": True},
        "reason": {"type": "Text", "nullable": False},
    },
    "operators": {
        "id": {"type": "Integer", "nullable": False},
        "username": {"type": "Text", "nullable": False},
        "password_hash": {"type": "Text", "nullable": False},
        "created_at": {"type": "DateTime", "nullable": False},
        "disabled_at": {"type": "DateTime", "nullable": True},
    },
    "quarantine": {
        "run_key": {"type": "Text", "nullable": False},
        "reason": {"type": "Text", "nullable": False},
        "verdicts": {"type": "JSONB", "nullable": False},
        "data_uri": {"type": "Text", "nullable": False},
        "resolved_as": {"type": "Text", "nullable": True},
        "actor": {"type": "Text", "nullable": True},
        "resolved_at": {"type": "DateTime", "nullable": True},
    },
    "resolutions": {
        "id": {"type": "Integer", "nullable": False},
        "run_key": {"type": "Text", "nullable": False},
        "target": {"type": "Text", "nullable": False},
        "field": {"type": "Text", "nullable": False},
        "winning_rung": {"type": "Integer", "nullable": True},
        "attempts": {"type": "JSONB", "nullable": False},
        "node_id": {"type": "Text", "nullable": True},
        "ms": {"type": "Integer", "nullable": False},
        "at": {"type": "DateTime", "nullable": False},
    },
    "run_attempts": {
        "id": {"type": "Integer", "nullable": False},
        "run_key": {"type": "Text", "nullable": False},
        "attempt": {"type": "Integer", "nullable": False},
        "started_at": {"type": "DateTime", "nullable": False},
        "finished_at": {"type": "DateTime", "nullable": True},
        "state": {"type": "Text", "nullable": False},
        "error": {"type": "Text", "nullable": True},
        "scope_record_sha256": {"type": "Text", "nullable": False},
    },
    "runs": {
        "run_key": {"type": "Text", "nullable": False},
        "target": {"type": "Text", "nullable": False},
        "spec_version": {"type": "Integer", "nullable": False},
        "window": {"type": "Date", "nullable": False},
        "first_started_at": {"type": "DateTime", "nullable": False},
        "last_finished_at": {"type": "DateTime", "nullable": True},
        "state": {"type": "Text", "nullable": False},
        "rows_written": {"type": "Integer", "nullable": True},
        "error": {"type": "Text", "nullable": True},
    },
    "scope_records": {
        "target": {"type": "Text", "nullable": False},
        "basis": {"type": "Text", "nullable": False},
        "authorising_party": {"type": "Text", "nullable": False},
        "authorised_on": {"type": "Date", "nullable": False},
        "expires_on": {"type": "Date", "nullable": False},
        "evidence_uri": {"type": "Text", "nullable": True},
        "evidence_sha256": {"type": "Text", "nullable": True},
        "origins": {"type": "JSONB", "nullable": False},
        "rate_limit": {"type": "JSONB", "nullable": False},
        "user_agent_contact": {"type": "Text", "nullable": False},
        "first_heal_policy": {"type": "Text", "nullable": False},
        "legal_review": {"type": "Text", "nullable": False},
        "warmup_runs": {"type": "Integer", "nullable": False},
        "evidence_retention_days": {"type": "Integer", "nullable": True},
        "record_sha256": {"type": "Text", "nullable": False},
        "loaded_at": {"type": "DateTime", "nullable": False},
        "credential_posture": {"type": "Text", "nullable": False},
    },
    "selector_cache": {
        "target": {"type": "Text", "nullable": False},
        "field": {"type": "Text", "nullable": False},
        "spec_version": {"type": "Integer", "nullable": False},
        "selector": {"type": "Text", "nullable": False},
        "anchor": {"type": "JSONB", "nullable": False},
        "node_id": {"type": "Text", "nullable": True},
        "set_by_rung": {"type": "Integer", "nullable": False},
        "set_at": {"type": "DateTime", "nullable": False},
        "set_by_run_key": {"type": "Text", "nullable": False},
    },
    "sessions": {
        "token_sha256": {"type": "CHAR", "nullable": False},
        "operator_id": {"type": "Integer", "nullable": False},
        "created_at": {"type": "DateTime", "nullable": False},
        "expires_at": {"type": "DateTime", "nullable": False},
        "revoked_at": {"type": "DateTime", "nullable": True},
    },
}


@pytest.mark.unit
def test_orm_metadata_matches_the_frozen_column_snapshot_exactly() -> None:
    actual = {
        table_name: {
            column.name: {"type": type(column.type).__name__, "nullable": column.nullable}
            for column in table.columns
        }
        for table_name, table in Base.metadata.tables.items()
    }
    assert actual == EXPECTED_COLUMNS


@pytest.mark.integration
def test_alembic_has_no_undetected_drift() -> None:
    """`alembic check` fails (non-zero exit) if the models and the migration files disagree --
    the tool this fix wave was asked to wire in as a test, not just a habit before merges."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ},
    )
    assert result.returncode == 0, (
        f"alembic check reported drift between selector_drift.db.models and "
        f"alembic/versions/:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
