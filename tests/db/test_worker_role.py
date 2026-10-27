"""Migration 0003: the worker's Postgres role, and an append-only `audit_log`.

The worker renders hostile HTML with --no-sandbox, so its database credential is the most
exposed one in the system. It used to be the compose superuser, which could rewrite `audit_log`
-- the table the rate limiter, the login throttle, the acknowledgement audit and the promotion
audit all rely on -- and relabel a scope record's basis. `sd_worker` can read and append to
`audit_log` and nothing else; it cannot touch `operators`, `sessions` or the migration table;
and it has no DDL.

Append-only is enforced by privilege, not by trigger: the test fixtures TRUNCATE and DELETE
`audit_log` as the administrator, and the administrator (the migrating role) stays able to. The
threat this closes is a compromised worker, not a compromised database owner.

Like tests/testbench/test_scorer_role.py, the tests become the role with `SET ROLE`, so the test
connection (SD_DATABASE_URL) must be a superuser or a member of sd_worker with SET.

Callers: none (pytest collects tests/db/test_*.py). Instruction: "Postgres role and append-onl".
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg.errors
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError

from selector_drift.db.session import engine

ROLE = "sd_worker"
AUDIT = "audit_log"
# What the worker must never reach: the API's credentials and sessions, and alembic's own table.
NEVER = {"operators", "sessions", "alembic_version"}
# The one table whose rows the product deletes (orchestrate.cache_writer).
DELETABLE = {"selector_cache"}
INSUFFICIENT_PRIVILEGE = "42501"
PROBE = "worker_role_probe"


def _engine_as(role: str) -> Engine:
    acting = create_engine(engine.url)

    @event.listens_for(acting, "connect")
    def _set_role(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET ROLE {role}")
        except psycopg.errors.InsufficientPrivilege:
            pytest.fail(
                "worker_engine needs a superuser (or SET membership in sd_worker); this reads "
                "like a grants bug but is a test-connection problem",
                pytrace=False,
            )
        finally:
            cursor.close()
        dbapi_connection.commit()

    return acting


@pytest.fixture
def worker_engine() -> Iterator[Engine]:
    acting = _engine_as(ROLE)
    yield acting
    acting.dispose()
    with engine.begin() as c:  # the administrator cleans up what the probe wrote
        c.execute(text("delete from audit_log where action = :a"), {"a": PROBE})


def _tables() -> list[str]:
    return sorted(inspect(engine).get_table_names())


def _has(privilege: str, table: str) -> bool:
    with engine.connect() as c:
        return bool(
            c.scalar(
                text("select has_table_privilege(:role, :table, :priv)"),
                {"role": ROLE, "table": table, "priv": privilege},
            )
        )


def _holding(privilege: str) -> set[str]:
    return {t for t in _tables() if _has(privilege, t)}


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
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0003_worker_role.py"
    ).read_text("utf-8")
    assert "ALTER ROLE" in source and "NOSUPERUSER" in source and "NOBYPASSRLS" in source


@pytest.mark.integration
def test_every_table_is_either_granted_to_the_worker_or_deliberately_withheld() -> None:
    """Fail closed for the next migration: a new table is neither silently open to the worker
    nor silently unusable by it. It has to be classified here, and in 0003's successor."""
    assert _holding("SELECT") == set(_tables()) - NEVER


@pytest.mark.integration
@pytest.mark.parametrize("privilege", ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"])
def test_the_worker_has_nothing_on_the_api_or_migration_tables(privilege: str) -> None:
    assert _holding(privilege) & NEVER == set()


@pytest.mark.integration
def test_audit_log_is_readable_and_appendable_and_nothing_more() -> None:
    assert _has("SELECT", AUDIT) and _has("INSERT", AUDIT)
    for privilege in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert not _has(privilege, AUDIT), privilege


@pytest.mark.integration
def test_the_data_tables_can_be_read_written_and_updated() -> None:
    data = set(_tables()) - NEVER - {AUDIT}
    assert _holding("INSERT") == data | {AUDIT}
    assert _holding("UPDATE") == data


@pytest.mark.integration
def test_only_the_selector_cache_can_have_rows_deleted() -> None:
    assert _holding("DELETE") == DELETABLE


@pytest.mark.integration
@pytest.mark.parametrize("privilege", ["TRUNCATE", "REFERENCES", "TRIGGER"])
def test_the_worker_has_no_structural_privilege_anywhere(privilege: str) -> None:
    assert _holding(privilege) == set()


@pytest.mark.integration
def test_the_worker_can_append_to_the_audit_log(worker_engine: Engine) -> None:
    with worker_engine.begin() as c:
        c.execute(
            text(
                "insert into audit_log (at, actor, action, subject, detail) "
                "values (now(), 'test', :a, 'x', '{}'::jsonb)"
            ),
            {"a": PROBE},
        )
        assert c.scalar(text("select count(*) from audit_log where action = :a"), {"a": PROBE}) == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    "statement",
    [
        "update audit_log set actor = 'someone-else'",
        "delete from audit_log",
        "truncate audit_log",
        "create table sneaky (id int)",
        "select * from operators",
        "select * from sessions",
        "alter table audit_log add column c int",
        "drop table audit_log",
    ],
)
def test_the_worker_is_refused(worker_engine: Engine, statement: str) -> None:
    with pytest.raises(ProgrammingError) as refused, worker_engine.begin() as c:
        c.execute(text(statement))
    assert refused.value.orig is not None
    assert refused.value.orig.sqlstate == INSUFFICIENT_PRIVILEGE, statement


@pytest.mark.integration
def test_every_granted_table_with_a_serial_id_has_a_usable_sequence() -> None:
    with engine.connect() as c:
        missing = [
            t
            for t in sorted(set(_tables()) - NEVER)
            if (seq := c.scalar(text("select pg_get_serial_sequence(:t, 'id')"), {"t": t}))
            and not c.scalar(
                text("select has_sequence_privilege(:r, :s, 'USAGE')"), {"r": ROLE, "s": seq}
            )
        ]
    assert missing == []
