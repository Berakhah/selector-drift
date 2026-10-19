"""bench scorer role

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19

Adds no table, column or index (the frozen day-one schema is untouched): it creates the Postgres
role the bench-scorer service connects as, and grants it exactly what testbench/score.py uses.
The role has no password here; the deploy sets it from .env (see .env.example).

A role is cluster-wide while every test worker migrates its own database, so creation is
idempotent and tolerates losing a race to another worker. The downgrade revokes the grants and
leaves the role: other databases in the cluster may still depend on it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None

ROLE = "sd_scorer"
# The scorer joins resolutions and reads audit_log to skip heals it already judged.
READS = ("runs", "heal_events", "quarantine", "resolutions", "audit_log")


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
    for table in READS:
        op.execute(f"GRANT SELECT ON {table} TO {ROLE}")
    op.execute(f"GRANT INSERT ON audit_log TO {ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE audit_log_id_seq TO {ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE USAGE ON SEQUENCE audit_log_id_seq FROM {ROLE}")
    op.execute(f"REVOKE INSERT ON audit_log FROM {ROLE}")
    for table in READS:
        op.execute(f"REVOKE SELECT ON {table} FROM {ROLE}")
