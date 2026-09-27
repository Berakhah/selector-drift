"""The frozen day-one schema. No later task may add a column to any table here --
day 1 must be comparable to day 90 across no migration. See CLAUDE.md and
`.superpowers/sdd/2024-11-07-selector-drift/task-3-brief.md`.
"""

from __future__ import annotations

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
    names = {i["name"] for i in inspect(engine).get_indexes("resolutions")}
    assert names, "resolutions is scanned by rung_stats on every publish (M15)"


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
