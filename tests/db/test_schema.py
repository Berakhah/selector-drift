"""The frozen day-one schema. No later task may add a column to any table here --
day 1 must be comparable to day 90 across no migration. See CLAUDE.md and
`.superpowers/sdd/2024-11-07-selector-drift/task-3-brief.md`.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import inspect

from selector_drift.db.session import engine

EXPECTED = {
    "runs",
    "run_attempts",
    "resolutions",
    "heal_events",
    "selector_cache",
    "quarantine",
    "audit_log",
    "scope_records",
    "deploys",
    "interventions",
    "operators",
    "sessions",
    "heartbeats",
    "findings",
}


@pytest.mark.integration
def test_all_tables_exist():
    assert set(inspect(engine).get_table_names()) >= EXPECTED


@pytest.mark.integration
def test_heal_events_carries_anchor_columns_from_day_one():
    cols = {c["name"] for c in inspect(engine).get_columns("heal_events")}
    assert {
        "anchor_before",
        "anchor_after",
        "confirmer_rung",
        "confirmer_family",
        "replayed_ok",
        "resolution_id",
    } <= cols


@pytest.mark.integration
def test_every_resolution_is_recorded_not_only_the_heals():
    """C4. rung_stats() is a published claim (B11) and heal_events only holds heals,
    so rung-1 wins would have no home and rung1 % would be uncomputable -- permanently,
    because this schema is frozen today."""
    cols = {c["name"] for c in inspect(engine).get_columns("resolutions")}
    assert {"run_key", "field", "winning_rung", "attempts", "node_id"} <= cols


@pytest.mark.integration
def test_a_retry_does_not_erase_the_failed_attempt():
    """M13. run_key is deterministic, so without run_attempts a retry overwrites the
    failure and inflates the published run-success rate."""
    cols = {c["name"] for c in inspect(engine).get_columns("run_attempts")}
    assert {"run_key", "attempt", "state", "error"} <= cols


@pytest.mark.integration
def test_the_cache_is_keyed_by_spec_version():
    """M14. Otherwise a spec edit silently reuses a selector resolved for the old meaning."""
    pk = set(inspect(engine).get_pk_constraint("selector_cache")["constrained_columns"])
    assert pk == {"target", "field", "spec_version"}


@pytest.mark.integration
def test_the_indexes_the_timeline_and_rung_stats_need_exist():
    """M15/P3, strengthened (M-6): all seven frozen indexes exist, by name and columns --
    not just "some index exists"."""
    insp = inspect(engine)
    expected = {
        "runs": {"ix_runs_first_started_at": ["first_started_at"]},
        "run_attempts": {"ix_run_attempts_run_key": ["run_key"]},
        "resolutions": {
            "ix_resolutions_run_key": ["run_key"],
            "ix_resolutions_target_field_at": ["target", "field", "at"],
        },
        "heal_events": {
            "ix_heal_events_run_key": ["run_key"],
            "ix_heal_events_target_broke_at": ["target", "broke_at"],
        },
        "deploys": {"ix_deploys_target_deployed_at": ["target", "deployed_at"]},
    }
    for table, indexes in expected.items():
        by_name = {i["name"]: i["column_names"] for i in insp.get_indexes(table)}
        for name, columns in indexes.items():
            assert by_name.get(name) == columns, f"{table}.{name}"


@pytest.mark.integration
def test_run_columns_are_typed_for_comparison_not_text():
    """I-1. `record_run(spec_version: int, window: date)` (Task 6), a Task 6 test asserts
    `run.window == date(2026, 9, 7)` and `run.spec_version == 1`, and text sorting on
    `spec_version` breaks M14 (text "9" sorts above "10"). Both columns must be typed, not
    Text."""
    cols = {c["name"]: c for c in inspect(engine).get_columns("runs")}
    assert cols["spec_version"]["type"].python_type is int
    assert cols["window"]["type"].python_type is dt.date


@pytest.mark.integration
def test_the_cache_orders_spec_version_numerically():
    """I-1. `SelectorCache` orders by `spec_version desc`; a text column sorts "9" above "10",
    silently picking the wrong (older) cache entry as "latest" -- breaking M14."""
    cols = {c["name"]: c for c in inspect(engine).get_columns("selector_cache")}
    assert cols["spec_version"]["type"].python_type is int


@pytest.mark.integration
def test_heal_events_resolution_id_is_nullable_for_replay():
    """I-2. `CascadeTrace.resolution_id` is `None` on replay -- a heal event replayed from
    stored evidence has no live `resolutions` row to join to."""
    cols = {c["name"]: c for c in inspect(engine).get_columns("heal_events")}
    assert cols["resolution_id"]["nullable"] is True


@pytest.mark.integration
def test_run_attempts_has_a_unique_constraint_on_run_key_and_attempt():
    """M-4. Without this, two racing writers (or a bug in the retry loop) could insert two
    rows for the same (run_key, attempt), corrupting the retry history run_attempts exists to
    keep accurate."""
    insp = inspect(engine)
    uniques = insp.get_unique_constraints("run_attempts")
    assert any(
        u["name"] == "uq_run_attempts_run_key_attempt"
        and set(u["column_names"]) == {"run_key", "attempt"}
        for u in uniques
    ), uniques


@pytest.mark.integration
def test_sessions_store_a_hash_and_never_a_token_column():
    """API spec 3. A database read must not yield a usable cookie."""
    cols = {c["name"] for c in inspect(engine).get_columns("sessions")}
    assert {"token_sha256", "operator_id", "created_at", "expires_at", "revoked_at"} <= cols
    assert not {"token", "session_token", "cookie"} & cols
    assert inspect(engine).get_pk_constraint("sessions")["constrained_columns"] == ["token_sha256"]


@pytest.mark.integration
def test_operators_have_a_unique_username_and_no_plaintext_password():
    cols = {c["name"] for c in inspect(engine).get_columns("operators")}
    assert {"id", "username", "password_hash", "created_at", "disabled_at"} <= cols
    assert "password" not in cols
    uniques = inspect(engine).get_unique_constraints("operators")
    indexes = inspect(engine).get_indexes("operators")
    assert any(u["column_names"] == ["username"] for u in uniques) or any(
        i["column_names"] == ["username"] and i["unique"] for i in indexes
    )


@pytest.mark.integration
def test_heartbeats_exist_on_day_one_indexed_by_target_and_slot():
    """API spec 6.1. The missed-run denominator. Missing on day one = missing for 90 days."""
    cols = {c["name"]: c for c in inspect(engine).get_columns("heartbeats")}
    assert {"id", "target", "scheduled_for", "fired_at", "run_key"} <= cols.keys()
    assert cols["run_key"]["nullable"] is True
    idx = {i["name"]: i["column_names"] for i in inspect(engine).get_indexes("heartbeats")}
    assert idx["ix_heartbeats_target_scheduled_for"] == ["target", "scheduled_for"]


@pytest.mark.integration
def test_findings_are_stored_so_the_quality_screen_has_a_column_behind_it():
    cols = {c["name"] for c in inspect(engine).get_columns("findings")}
    assert {"id", "run_key", "severity", "rule", "payload", "at"} <= cols


@pytest.mark.integration
def test_the_scope_mirror_carries_warmup_and_retention():
    cols = {c["name"]: c for c in inspect(engine).get_columns("scope_records")}
    assert cols["warmup_runs"]["nullable"] is False
    assert cols["evidence_retention_days"]["nullable"] is True


@pytest.mark.integration
def test_a_run_row_may_exist_before_it_finishes():
    cols = {c["name"]: c["nullable"] for c in inspect(engine).get_columns("runs")}
    assert {k: cols[k] for k in ("last_finished_at", "rows_written", "error")} == {
        "last_finished_at": True,
        "rows_written": True,
        "error": True,
    }
    assert not any(
        cols[k]
        for k in ("run_key", "target", "spec_version", "window", "first_started_at", "state")
    )


@pytest.mark.integration
def test_deploys_are_keyed_by_target_time_and_ref():
    pk = inspect(engine).get_pk_constraint("deploys")["constrained_columns"]
    assert set(pk) == {"target", "deployed_at", "ref"}
