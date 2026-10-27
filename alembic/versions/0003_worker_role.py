"""worker role, and an append-only audit_log

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27

Adds no table, column or index. It creates the Postgres role the worker connects as, so the
process that renders hostile HTML no longer holds the compose superuser's credential.

`audit_log` is the rate limiter's bucket, the login throttle, the acknowledgement audit and the
promotion audit. The worker gets SELECT and INSERT on it and nothing else: no UPDATE, DELETE or
TRUNCATE, so a compromised worker cannot erase what it did or reset its own rate limit. This is
privilege, not a trigger, because the administrator (the migrating role, and the test fixtures)
must stay able to clear it. It gets no access at all to `operators`, `sessions` (the API's
password hashes and session tokens) or `alembic_version`, and no DDL.

Every other table is readable, insertable and updatable; only `selector_cache` may have rows
deleted (orchestrate.cache_writer is the one place the product deletes). UPDATE is granted
broadly rather than per column: tests/orchestrate/test_worker_role_pipeline.py runs the real
product as this role, so a grant that is too narrow fails in CI, not at 04:12 on the VM.

The role has no password here; the deploy sets it from .env (see .env.example). A role is
cluster-wide while every test worker migrates its own database, so creation is idempotent and
tolerates losing a race. The downgrade revokes the grants and leaves the role.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None

ROLE = "sd_worker"
AUDIT = "audit_log"
# Read, insert and update. A table added later is NOT granted automatically: tests/db/
# test_worker_role.py fails until the next migration classifies it.
DATA = (
    "deploys",
    "findings",
    "heal_events",
    "heartbeats",
    "interventions",
    "quarantine",
    "resolutions",
    "run_attempts",
    "runs",
    "scope_records",
    "selector_cache",
)
DELETABLE = ("selector_cache",)


def _owned_sequences(tables: Sequence[str], verb: str, preposition: str) -> str:
    """`GRANT USAGE ... TO` / `REVOKE USAGE ... FROM` the role, over every sequence owned by a
    column of `tables`. Found through pg_depend: pg_get_serial_sequence(t, 'id') raises for a
    table with no `id` column. Built with quote_ident rather than format('%I'): a literal `%`
    in text passed through SQLAlchemy and psycopg is one more thing to get wrong."""
    names = ", ".join(f"'{t}'" for t in tables)
    return f"""
        DO $$
        DECLARE s record;
        BEGIN
            FOR s IN
                SELECT n.nspname AS schema, c.relname AS seq
                FROM pg_class c
                JOIN pg_depend d ON d.objid = c.oid AND d.deptype IN ('a', 'i')
                JOIN pg_class t ON t.oid = d.refobjid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'S' AND n.nspname = 'public' AND t.relname IN ({names})
            LOOP
                EXECUTE '{verb} USAGE ON SEQUENCE ' || quote_ident(s.schema) || '.'
                    || quote_ident(s.seq) || ' {preposition} {ROLE}';
            END LOOP;
        END
        $$
    """


def upgrade() -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} LOGIN;
            END IF;
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;
        END
        $$
    """)
    # The DO block skips a role that already exists: reset any powers it was given elsewhere.
    op.execute(
        f"ALTER ROLE {ROLE} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS LOGIN"
    )
    for table in DATA:
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {ROLE}")
    for table in DELETABLE:
        op.execute(f"GRANT DELETE ON {table} TO {ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON {AUDIT} TO {ROLE}")
    op.execute(_owned_sequences((*DATA, AUDIT), "GRANT", "TO"))


def downgrade() -> None:
    op.execute(_owned_sequences((*DATA, AUDIT), "REVOKE", "FROM"))
    op.execute(f"REVOKE SELECT, INSERT ON {AUDIT} FROM {ROLE}")
    for table in DELETABLE:
        op.execute(f"REVOKE DELETE ON {table} FROM {ROLE}")
    for table in DATA:
        op.execute(f"REVOKE SELECT, INSERT, UPDATE ON {table} FROM {ROLE}")
