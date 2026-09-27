"""The full run and heal-event schema -- frozen on day one of the ninety-day log.

No later task may add a column to any table declared here: day 1 must be comparable to day 90
across zero schema migrations. See CLAUDE.md ("The ninety-day log") and
`.superpowers/sdd/2024-11-07-selector-drift/task-3-brief.md`.

`window` is a reserved word in PostgreSQL (used in window functions); SQLAlchemy's PostgreSQL
identifier preparer quotes it automatically wherever it appears as a column or table name, so
`Run.window` needs no special handling here. Any hand-written SQL elsewhere (`seed.sql`,
`derive.py`, `testbench/score.py`, `psql` checks) must still write `"window"` explicitly.

All timestamps are `timestamptz` (`DateTime(timezone=True)`), never naive. All JSON columns are
JSONB, never plain JSON.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import CHAR, Boolean, Date, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class Run(Base):
    """The terminal record of one run. `run_key` is deterministic
    (`f"{target}:{spec_version}:{window}"`), so this row is created once per logical run and
    updated in place as the run progresses -- `run_attempts` keeps the retry history this
    table cannot (M13)."""

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_first_started_at", "first_started_at"),)

    run_key: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    spec_version: Mapped[str] = mapped_column(Text, nullable=False)
    window: Mapped[str] = mapped_column(Text, nullable=False)
    first_started_at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    last_finished_at: Mapped[dt.datetime | None] = mapped_column(_TZ, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    rows_written: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RunAttempt(Base):
    """One row per attempt at a run_key. A retry after a failure creates a new row here
    rather than overwriting the failed one, so the published run-success rate is computable
    from history rather than from whatever `runs` last happened to hold (M13)."""

    __tablename__ = "run_attempts"
    __table_args__ = (Index("ix_run_attempts_run_key", "run_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=False)
    run_key: Mapped[str] = mapped_column(Text, ForeignKey("runs.run_key"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(_TZ, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_record_sha256: Mapped[str] = mapped_column(Text, nullable=False)


class Resolution(Base):
    """One row per field resolution attempted, whether or not it healed. `heal_events` only
    holds heals, so rung-1 wins -- the overwhelming majority -- have no home except here; this
    is the denominator `rung_stats()` (Task 6) and the published B11 claim need (C4)."""

    __tablename__ = "resolutions"
    __table_args__ = (
        Index("ix_resolutions_run_key", "run_key"),
        Index("ix_resolutions_target_field_at", "target", "field", "at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=False)
    run_key: Mapped[str] = mapped_column(Text, ForeignKey("runs.run_key"), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    # Null means no rung resolved the field at all (every rung, including rung 5, failed).
    winning_rung: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ms: Mapped[int] = mapped_column(Integer, nullable=False)
    at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)


class HealEvent(Base):
    """One row per successful heal. Joinable back to the `Resolution` that produced it via
    `resolution_id`, and carries redacted before/after evidence so a heal can be replayed
    (heallog spec) rather than trusted as an anecdote."""

    __tablename__ = "heal_events"
    __table_args__ = (
        Index("ix_heal_events_run_key", "run_key"),
        Index("ix_heal_events_target_broke_at", "target", "broke_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=False)
    run_key: Mapped[str] = mapped_column(Text, ForeignKey("runs.run_key"), nullable=False)
    resolution_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resolutions.id"), nullable=False
    )
    target: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    broke_at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    winning_rung: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmer_rung: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmer_family: Mapped[str | None] = mapped_column(Text, nullable=True)
    selector_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    selector_after: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    anchor_after: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trace: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    verdicts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    replayed_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    promoted_at: Mapped[dt.datetime | None] = mapped_column(_TZ, nullable=True)


class SelectorCacheEntry(Base):
    """Keyed by `(target, field, spec_version)` (M14): a `FieldTarget.description` edit
    changes what the field *means*, and without `spec_version` in the key the next run would
    take a rung-1 cache hit on a selector resolved for the old meaning, with the old anchor
    verifying happily against the wrong thing."""

    __tablename__ = "selector_cache"

    target: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    field: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    spec_version: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    selector: Mapped[str] = mapped_column(Text, nullable=False)
    anchor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    set_by_rung: Mapped[int] = mapped_column(Integer, nullable=False)
    set_at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    set_by_run_key: Mapped[str] = mapped_column(Text, nullable=False)


class Quarantine(Base):
    """A quarantined run keeps its data, evidence and diff (CLAUDE.md: "quarantine, not
    failure") and offers one-click promote or discard -- `resolved_as` and `actor` record
    which happened."""

    __tablename__ = "quarantine"

    run_key: Mapped[str] = mapped_column(
        Text, ForeignKey("runs.run_key"), primary_key=True, nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    verdicts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    data_uri: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_as: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(_TZ, nullable=True)


class AuditLog(Base):
    """Append-only. Every gate refusal, promotion, discard and manual action is a row here."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=False)
    at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ScopeRecordRow(Base):
    """The persisted mirror of `ScopeRecord` (Task 2), keyed by `target`. `basis` and
    `first_heal_policy` are stored as their plain string enum values -- no Postgres ENUM type,
    which is painful to migrate. `origins` and `rate_limit` are stored as JSONB, `rate_limit`
    as `{"requests": int, "per_seconds": int}`.

    `credential_posture` arrives as a `ScopeRecord` field only in Task 32, but the column is
    here now because the schema freezes on day one; it defaults to `'session_injection'`, the
    safest posture in the preference order CLAUDE.md sets out."""

    __tablename__ = "scope_records"

    target: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    authorising_party: Mapped[str] = mapped_column(Text, nullable=False)
    authorised_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    expires_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    evidence_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    origins: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    rate_limit: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    user_agent_contact: Mapped[str] = mapped_column(Text, nullable=False)
    first_heal_policy: Mapped[str] = mapped_column(Text, nullable=False)
    legal_review: Mapped[str] = mapped_column(Text, nullable=False)
    warmup_runs: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    record_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    loaded_at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    credential_posture: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="session_injection"
    )


class Deploy(Base):
    """The control arm's ground truth (M10, R17): design 8.1's claim that heals fired only on
    runs following an actual deploy is unverifiable without a stored feed. Keyed by
    `(target, deployed_at, ref)` -- two deploys of one target in the same second with
    different refs are two deploys (found 2026-09-26: an unkeyed table let a re-ingested feed
    duplicate rows)."""

    __tablename__ = "deploys"
    __table_args__ = (Index("ix_deploys_target_deployed_at", "target", "deployed_at"),)

    target: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    deployed_at: Mapped[dt.datetime] = mapped_column(_TZ, primary_key=True, nullable=False)
    ref: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    ingested_at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)


class Intervention(Base):
    """Every manual fix -- spec edit, cache edit, manual promote -- is a row here. Without
    this table "zero manual selector fixes" has the same epistemic status as the vendor
    numbers `00` was written to reject (M1, R17)."""

    __tablename__ = "interventions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=False)
    at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str | None] = mapped_column(Text, nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class Operator(Base):
    """API spec 3. Holds no log data; the username is unique and the password is never stored
    in plaintext, only its hash."""

    __tablename__ = "operators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    disabled_at: Mapped[dt.datetime | None] = mapped_column(_TZ, nullable=True)


class OperatorSession(Base):
    """API spec 3. Named `OperatorSession` (table `sessions`), not `Session` -- that name is
    reserved for `db.session.Session`, the sessionmaker. Stores only the SHA-256 of the
    session cookie, never the cookie itself, so a database read never yields a usable
    session."""

    __tablename__ = "sessions"

    token_sha256: Mapped[str] = mapped_column(CHAR(64), primary_key=True, nullable=False)
    operator_id: Mapped[int] = mapped_column(Integer, ForeignKey("operators.id"), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(_TZ, nullable=True)


class Heartbeat(Base):
    """API spec 6.1. Written before any `runs` row exists -- a fire whose spec did not load
    has no run key at all, so `run_key` is nullable and carries no FK. This is the denominator
    of the published missed-run count, so it must exist from day one: missing on day one is
    missing for all ninety days."""

    __tablename__ = "heartbeats"
    __table_args__ = (Index("ix_heartbeats_target_scheduled_for", "target", "scheduled_for"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_for: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    fired_at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
    run_key: Mapped[str | None] = mapped_column(Text, nullable=True)


class FindingRow(Base):
    """Found 2026-09-26, not in the API spec: Task 29's `evaluate()` returns
    `list[Finding]` and nothing stored them, so `GET /quality` and
    `RunDetailView.finding_count` would have no column behind them (API spec 1, rule 1).
    Named `FindingRow` (table `findings`) -- `Finding` collides with `bok_core.findings.Finding`.
    `payload` is the whole `Finding`, `model_dump(mode="json")`."""

    __tablename__ = "findings"
    __table_args__ = (Index("ix_findings_run_key", "run_key"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    run_key: Mapped[str] = mapped_column(Text, ForeignKey("runs.run_key"), nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    rule: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    at: Mapped[dt.datetime] = mapped_column(_TZ, nullable=False)
