"""Migration 0002: the bench scorer's Postgres role. It is cluster-wide, so these tests switch
role with `SET ROLE` on a dedicated engine instead of logging in (a login needs a password, and
parallel test workers would race on it). The last test is the one that matters: the real scorer,
run with nothing but the role's grants, so the grant list is exact."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.session import engine
from testbench.score import score_pending
from tests.testbench.conftest import SeededHeal

ROLE = "sd_scorer"
READABLE = {"runs", "heal_events", "quarantine", "resolutions", "audit_log"}
INSUFFICIENT_PRIVILEGE = "42501"


@pytest.fixture
def scorer_engine() -> Iterator[Engine]:
    scorer = create_engine(engine.url)

    @event.listens_for(scorer, "connect")
    def _as_scorer(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET ROLE {ROLE}")
        cursor.close()
        dbapi_connection.commit()

    yield scorer
    scorer.dispose()


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
            text("select rolcanlogin, rolsuper, rolcreatedb, rolcreaterole "
                 "from pg_roles where rolname = :r"),
            {"r": ROLE},
        ).one()  # fmt: skip
    assert tuple(row) == (True, False, False, False)


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
