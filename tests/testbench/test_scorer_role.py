"""Migration 0002: the bench scorer's Postgres role. It is cluster-wide, so these tests switch
role with `SET ROLE` on a dedicated engine instead of logging in (a login needs a password, and
parallel test workers would race on it). The last test is the one that matters: the real scorer,
run with nothing but the role's grants, so the grant list is exact.

The test connection (SD_DATABASE_URL) must be a superuser, or a member of sd_scorer with SET: the
tests become the role with `SET ROLE`. On PG16 a non-superuser role owner gets ADMIN OPTION only,
which does not allow SET ROLE, so a plain owner connection fails with "permission denied to set
role" -- a test-connection problem, not a grants bug."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg.errors
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.session import engine
from testbench.score import ScorerNotReady, check_scorer_access, score_pending
from tests.testbench.conftest import SeededHeal

ROLE = "sd_scorer"
READABLE = {"runs", "heal_events", "quarantine", "resolutions", "audit_log"}
INSUFFICIENT_PRIVILEGE = "42501"


def _engine_as(role: str) -> Engine:
    acting = create_engine(engine.url)

    @event.listens_for(acting, "connect")
    def _set_role(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET ROLE {role}")
        except psycopg.errors.InsufficientPrivilege:
            pytest.fail(
                "scorer_engine needs a superuser (or SET membership in sd_scorer); this reads "
                "like a grants bug but is a test-connection problem",
                pytrace=False,
            )
        finally:
            cursor.close()
        dbapi_connection.commit()

    return acting


@pytest.fixture
def scorer_engine() -> Iterator[Engine]:
    scorer = _engine_as(ROLE)
    yield scorer
    scorer.dispose()


@pytest.fixture
def throwaway_role() -> Iterator[Callable[..., Engine]]:
    """Roles are cluster-wide and workers run in parallel: a uuid-suffixed name, dropped after."""
    made: list[tuple[str, Engine]] = []

    def make(*grants: str) -> Engine:
        name = f"sd_t_{uuid.uuid4().hex[:12]}"
        with engine.begin() as c:
            c.execute(text(f"CREATE ROLE {name} NOLOGIN"))
            for grant in grants:
                c.execute(text(grant.format(role=name)))
        acting = _engine_as(name)
        made.append((name, acting))
        return acting

    yield make
    for name, acting in made:
        acting.dispose()
        with engine.begin() as c:
            c.execute(text(f"DROP OWNED BY {name}"))
            c.execute(text(f"DROP ROLE {name}"))


def _all_tables() -> list[str]:
    return sorted(inspect(engine).get_table_names())


def _has(privilege: str, table: str) -> bool:
    with engine.connect() as c:
        return bool(
            c.scalar(
                text("select has_table_privilege(:role, :table, :priv)"),
                {"role": ROLE, "table": table, "priv": privilege},
            )
        )


@pytest.mark.integration
def test_the_role_can_log_in_in_principle_and_has_no_other_powers() -> None:
    with engine.connect() as c:
        row = c.execute(
            text("select rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                 "rolreplication, rolbypassrls from pg_roles where rolname = :r"),
            {"r": ROLE},
        ).one()  # fmt: skip
    assert tuple(row) == (True, False, False, False, False, False)


@pytest.mark.unit
def test_the_migration_resets_a_pre_existing_roles_powers() -> None:
    """The create is skipped for an existing role, so the migration also ALTERs it. Asserted on
    the source: tampering with the cluster-wide role would disturb parallel workers."""
    source = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0002_bench_scorer_role.py"
    ).read_text("utf-8")
    assert "ALTER ROLE" in source and "NOSUPERUSER" in source and "NOBYPASSRLS" in source


@pytest.mark.integration
def test_select_is_granted_on_exactly_the_five_tables_the_scorer_reads() -> None:
    assert {t for t in _all_tables() if _has("SELECT", t)} == READABLE


@pytest.mark.integration
def test_insert_is_granted_on_audit_log_only() -> None:
    assert {t for t in _all_tables() if _has("INSERT", t)} == {"audit_log"}


@pytest.mark.integration
@pytest.mark.parametrize("privilege", ["UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"])
def test_nothing_else_is_granted_anywhere(privilege: str) -> None:
    assert [t for t in _all_tables() if _has(privilege, t)] == []


@pytest.mark.integration
def test_the_audit_log_id_sequence_is_usable() -> None:
    with engine.connect() as c:
        usable = c.scalar(
            text(
                "select has_sequence_privilege("
                ":r, pg_get_serial_sequence('audit_log', 'id'), 'USAGE')"
            ),
            {"r": ROLE},
        )
    assert usable is True


@pytest.mark.integration
@pytest.mark.parametrize(
    "statement",
    [
        "delete from audit_log",
        "update audit_log set actor = 'x'",
        "truncate audit_log",
        "insert into runs default values",
        "update heal_events set field = 'x'",
        "delete from quarantine",
        "select * from selector_cache",
        "select * from scope_records",
    ],
)
def test_a_scorer_connection_is_refused_everything_outside_its_grants(
    scorer_engine: Engine, statement: str
) -> None:
    with pytest.raises(ProgrammingError) as caught, scorer_engine.begin() as c:
        c.execute(text(statement))
    assert caught.value.orig.sqlstate == INSUFFICIENT_PRIVILEGE  # type: ignore[union-attr]


@pytest.mark.integration
def test_the_preflight_passes_as_the_scorer(scorer_engine: Engine, tmp_path: Path) -> None:
    with OrmSession(scorer_engine) as s:
        check_scorer_access(s, tmp_path)


@pytest.mark.integration
def test_the_preflight_refuses_a_role_with_no_grants(
    throwaway_role: Callable[..., Engine], tmp_path: Path
) -> None:
    acting = throwaway_role()
    with OrmSession(acting) as s, pytest.raises(ScorerNotReady, match="cannot read audit_log"):
        check_scorer_access(s, tmp_path)


@pytest.mark.integration
def test_the_preflight_refuses_a_role_that_cannot_insert(
    throwaway_role: Callable[..., Engine], tmp_path: Path
) -> None:
    acting = throwaway_role("GRANT SELECT ON audit_log TO {role}")
    with OrmSession(acting) as s, pytest.raises(ScorerNotReady, match="cannot insert"):
        check_scorer_access(s, tmp_path)


@pytest.mark.integration
def test_the_whole_scorer_runs_with_nothing_but_the_roles_grants(
    db: OrmSession,
    seeded_bench_heals: list[SeededHeal],
    redaction_key: bytes,
    scorer_engine: Engine,
) -> None:
    with OrmSession(scorer_engine) as s:
        scored = score_pending(s, redaction_key=redaction_key)
    assert sorted(scored) == sorted(h.id for h in seeded_bench_heals)
    with engine.connect() as c:
        rows = c.execute(
            text("select subject, detail->>'judgement' from audit_log "
                 "where actor = 'testbench.score' and action = 'scored'")
        ).all()  # fmt: skip
    assert {(int(subject), judgement) for subject, judgement in rows} == {
        (h.id, h.expected) for h in seeded_bench_heals
    }
