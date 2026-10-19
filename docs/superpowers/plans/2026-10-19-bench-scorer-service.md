# Bench-Scorer Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the Task 26C bench scorer in production as its own hardened compose service, so a false-accept count exists and the Task 29A alert can fire.

**Architecture:** A new `bench-scorer` compose service built from `docker/Dockerfile.bench-scorer` (selector_drift + testbench, no browsers) serves the existing `bench-scorer` Prefect deployment through a bench-side module, `testbench/serve_scorer.py`. It connects to Postgres as a new narrow role `sd_scorer` (created by migration 0002), reads only the `evidence` subpath of `sd_data` read-only, and holds `SD_REDACTION_KEY`.

**Tech Stack:** Python 3.12, uv, Prefect 3 `serve()`, SQLAlchemy 2 + psycopg 3, alembic, Postgres 16, Docker Compose, pytest, import-linter.

**Spec:** `docs/superpowers/specs/2026-10-19-bench-scorer-service-design.md`

## Global Constraints

- The product never imports `testbench` (contract `answer-key-isolation`); `testbench.serve_scorer` may import only `prefect` and `testbench.score` (contract `only-the-scorer-reads-the-product` counts indirect imports).
- No secret in the repo or in an image: `SD_REDACTION_KEY` and `SD_SCORER_PASSWORD` come from `.env` in the fail-closed `${VAR:?message}` form, never `${VAR:-default}`.
- The frozen day-one schema (`tests/db/test_schema.py`, `test_schema_snapshot.py`) must stay green: migration 0002 adds no table, column or index.
- Same container hardening as the other services: `read_only: true`, `user: "10001:10001"`, `cap_drop: [ ALL ]`, `no-new-privileges:true`, `tmpfs: [ "/tmp" ]`, `mem_limit`, `pids_limit`, no published ports.
- `mypy --strict` (over `selector_drift` and `testbench`), `ruff check`, `ruff format --check`, and `lint-imports` (all contracts kept) clean after every task.
- TDD: RED, GREEN, REFACTOR, no exceptions. Run tests with `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m pytest ...` unless `uv` is on PATH.
- Integration tests need `SD_DATABASE_URL` naming a reachable `*_test` database (`tests/conftest.py` fails fast otherwise).
- Commit message trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `alembic/versions/0002_bench_scorer_role.py` (create) | Create role `sd_scorer` idempotently; grant exactly what the scorer uses |
| `tests/testbench/test_scorer_role.py` (create) | Privilege matrix, refused statements, and the real scorer run as `sd_scorer` (lives in `tests/testbench/` to reuse its `db`, `seeded_bench_heals`, `redaction_key` fixtures) |
| `docker-compose.yml` (modify) | Forward `SD_REDACTION_KEY` to the worker; add the `bench-scorer` service |
| `tests/test_compose_fails_closed.py` (modify) | Pin the fail-closed secrets |
| `tests/testbench/test_scorer_service.py` (create) | Pin the scorer service's hardening, mounts and user |
| `testbench/serve_scorer.py` (create) | Bench-side entrypoint: build and `serve()` the `bench-scorer` deployment |
| `tests/testbench/test_serve_scorer.py` (create) | The deployment agrees with `deployments.BENCH_SCORER` |
| `docker/Dockerfile.bench-scorer` (create) | Image for the scorer service |
| `.env.example` (modify) | `SD_SCORER_PASSWORD` and the one-time `ALTER ROLE` step |
| `selector_drift/orchestrate/deployments.py` (modify) | Worker's missing-scorer message: error becomes info |

---

### Task 1: The `sd_scorer` role and its grants

**Files:**
- Create: `alembic/versions/0002_bench_scorer_role.py`
- Create: `tests/testbench/test_scorer_role.py`

**Interfaces:**
- Consumes: `selector_drift.db.session.engine`; fixtures `db`, `seeded_bench_heals`, `redaction_key` from `tests/testbench/conftest.py`; `testbench.score.score_pending(session, *, redaction_key)`.
- Produces: Postgres role `sd_scorer` (cluster-wide, `LOGIN`, no password) with `SELECT` on `runs, heal_events, quarantine, resolutions, audit_log`, `INSERT` on `audit_log`, `USAGE` on `audit_log_id_seq`. Later tasks connect as this role.

- [ ] **Step 1: Write the failing tests**

Create `tests/testbench/test_scorer_role.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m pytest tests/testbench/test_scorer_role.py -q -p no:cacheprovider`
Expected: FAIL. The first test fails with `NoResultFound` (role missing) and the `SET ROLE` ones with `role "sd_scorer" does not exist`. If instead the whole file errors with `DatabaseUnreachableError` or `NotATestDatabaseError`, set `SD_DATABASE_URL` to a reachable `*_test` database and migrate it (`alembic upgrade head`) first.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0002_bench_scorer_role.py`:

```python
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
```

- [ ] **Step 4: Apply the migration and run the tests to verify they pass**

Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m alembic upgrade head` (against the `*_test` database), then
`.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m pytest tests/testbench/test_scorer_role.py -q -p no:cacheprovider`
Expected: PASS. If the last test fails with `InsufficientPrivilege` naming a table, that table belongs in `READS`: add it to the migration, to the test's `READABLE` constant, and to spec section 4 in the same commit. If `test_select_is_granted_...` finds extra tables, the role inherited a grant it should not have: find where it comes from before touching the test.
If `SET ROLE` itself is refused, the connecting user is not a superuser or a member of `sd_scorer`: use the compose Postgres user (`POSTGRES_USER`) for `SD_DATABASE_URL`.

- [ ] **Step 5: Verify nothing else moved**

Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m pytest tests/db -q -p no:cacheprovider`
Expected: PASS, including `test_alembic_has_no_undetected_drift` and the frozen-schema snapshot.
Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m ruff check alembic tests/testbench && .claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m ruff format --check alembic tests/testbench`
Expected: clean. (If `ruff format` rewrites the migration's SQL f-string, accept its layout.)

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0002_bench_scorer_role.py tests/testbench/test_scorer_role.py
git commit -m "feat(db): sd_scorer role with exactly the grants the bench scorer uses"
```

---

### Task 2: Forward the redaction key (worker and scorer) fail-closed

**Files:**
- Modify: `docker-compose.yml` (worker `environment`, lines 71-86)
- Modify: `tests/test_compose_fails_closed.py`
- Modify: `.env.example` (line 25 area)

**Interfaces:**
- Consumes: compose `worker` service.
- Produces: the worker receives `SD_REDACTION_KEY: ${SD_REDACTION_KEY:?...}`; Task 4's scorer service uses the identical form, and the test there pins that both read the same variable.

Found while planning: compose forwards `SD_REDACTION_KEY` to no service today, so the worker raises `ConfigError("SD_REDACTION_KEY is not set; evidence cannot be redacted")` on the first heal it stores. This task fixes that existing gap, in its own commit.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_compose_fails_closed.py`:

```python
@pytest.mark.unit
def test_the_worker_receives_the_redaction_key_and_fails_closed_without_it() -> None:
    env = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["worker"]["environment"]
    value = env["SD_REDACTION_KEY"]
    assert re.fullmatch(r"\$\{SD_REDACTION_KEY:\?\S.*\}", value), (
        "the worker's SD_REDACTION_KEY must use the fail-closed ${VAR:?message} form; "
        f"got {value!r}"
    )
```

and add `import yaml` to the file's imports (after `import pytest`).

- [ ] **Step 2: Run it to verify it fails**

Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m pytest tests/test_compose_fails_closed.py -q -p no:cacheprovider`
Expected: FAIL with `KeyError: 'SD_REDACTION_KEY'`.

- [ ] **Step 3: Forward the key**

In `docker-compose.yml`, in the `worker` service's `environment`, after the `SD_EVIDENCE_ROOT: /data/evidence` line add:

```yaml
      SD_REDACTION_KEY: ${SD_REDACTION_KEY:?set SD_REDACTION_KEY in .env -- see .env.example}
```

In `.env.example` change the key's comment so it says it is required and shared:

```
SD_REDACTION_KEY=          # REQUIRED; generate: python -c "import secrets; print(secrets.token_hex(32))". The worker and the bench-scorer must hold the SAME value.
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m pytest tests/test_compose_fails_closed.py tests/orchestrate/test_deployments.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml tests/test_compose_fails_closed.py .env.example
git commit -m "fix(compose): forward SD_REDACTION_KEY to the worker, fail closed

docker-compose.yml passed the key to no service, so the worker could not redact
evidence in compose."
```

---

### Task 3: `testbench/serve_scorer.py`

**Files:**
- Create: `testbench/serve_scorer.py`
- Create: `tests/testbench/test_serve_scorer.py`
- Modify: `selector_drift/orchestrate/deployments.py:126-131` (soften the worker's message)

**Interfaces:**
- Consumes: `testbench.score.score_pending_flow`; `selector_drift.orchestrate.deployments.BENCH_SCORER` (tests only).
- Produces: `testbench.serve_scorer.scorer_deployment() -> RunnerDeployment` and `testbench.serve_scorer.main() -> None` (the compose command is `python -m testbench.serve_scorer`).

- [ ] **Step 1: Write the failing tests**

Create `tests/testbench/test_serve_scorer.py`:

```python
"""The scorer's own entrypoint (bench-scorer compose service). It builds the deployment from the
flow itself, because importing selector_drift.orchestrate.deployments would be an indirect path
to selector_drift.db (contract only-the-scorer-reads-the-product). These tests pin that the
duplicated name and cron cannot drift from the worker's declaration."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_the_deployment_agrees_with_the_one_the_worker_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from selector_drift.orchestrate.deployments import BENCH_SCORER
    from testbench.serve_scorer import scorer_deployment

    monkeypatch.chdir(REPO)
    d = scorer_deployment()
    assert d.name == BENCH_SCORER["name"]
    assert d.flow_name == "bench-scorer"
    (schedule,) = d.schedules
    assert schedule.schedule.cron == BENCH_SCORER["cron"]
    assert schedule.schedule.timezone == BENCH_SCORER["timezone"]
    assert d.entrypoint is not None
    assert d.entrypoint.replace("\\", "/") == BENCH_SCORER["entrypoint"]


@pytest.mark.unit
def test_main_serves_exactly_the_one_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    import testbench.serve_scorer as module

    served: list[object] = []
    monkeypatch.setattr(module, "serve", lambda *deployments: served.extend(deployments))
    monkeypatch.chdir(REPO)
    module.main()
    assert [d.name for d in served] == ["bench-scorer"]  # type: ignore[attr-defined]


@pytest.mark.unit
def test_it_imports_only_prefect_and_the_scorer() -> None:
    """The import contract counts indirect imports; a bench module may reach selector_drift.db
    only through testbench.score."""
    tree = ast.parse((REPO / "testbench" / "serve_scorer.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    allowed = {
        "__future__",
        "logging",
        "typing",
        "prefect",
        "prefect.deployments.runner",
        "prefect.schedules",
        "testbench.score",
    }
    assert imported <= allowed, imported - allowed
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m pytest tests/testbench/test_serve_scorer.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'testbench.serve_scorer'`.

- [ ] **Step 3: Write the module**

Create `testbench/serve_scorer.py`:

```python
"""Serves the bench scorer from its own container (the `bench-scorer` compose service).

The product worker image ships selector_drift/ only, so it cannot serve the scorer
(orchestrate/deployments.py logs that and carries on). This module is the bench-side entrypoint:
it builds the same `bench-scorer` deployment from the flow itself and serves it. It imports only
prefect and testbench.score -- selector_drift.orchestrate.deployments would be an indirect path to
selector_drift.db, which contract only-the-scorer-reads-the-product allows for testbench.score
alone. The name, cron and timezone repeat deployments.BENCH_SCORER; test_serve_scorer pins that
the two agree.

A missing or broken testbench.score is an ImportError at start-up: the service exits non-zero
rather than serving nothing.
"""

from __future__ import annotations

import logging
from typing import cast

from prefect import serve
from prefect.deployments.runner import RunnerDeployment
from prefect.schedules import Cron

from testbench.score import score_pending_flow

NAME = "bench-scorer"
CRON = "42 4 * * *"  # 30 minutes after the bench's 04:12 run
TIMEZONE = "UTC"


def scorer_deployment() -> RunnerDeployment:
    # `to_deployment` is `@sync_compatible`: mypy sees a union with a coroutine although this
    # synchronous call always returns a RunnerDeployment (same cast as orchestrate/deployments.py).
    return cast(
        RunnerDeployment,
        score_pending_flow.to_deployment(
            name=NAME,
            schedules=[Cron(CRON, timezone=TIMEZONE)],
            tags=["bench"],
            description="daily, after the bench run: judge every bench heal against the answer key",
        ),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    serve(scorer_deployment())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m pytest tests/testbench/test_serve_scorer.py -q -p no:cacheprovider`
Expected: PASS. If the first test fails only on `d.entrypoint`, print `d.entrypoint`: if Prefect derived an absolute or differently-rooted path, the container's working directory (`/app`) decides the real value, so compare against the path *relative to REPO* and say so in the test; never loosen the assertion to `endswith` without a comment explaining why.

- [ ] **Step 5: Soften the worker's message and run the import contracts**

In `selector_drift/orchestrate/deployments.py`, replace the `_log.error(...)` block in `main()`:

```python
    scorer = bench_scorer_deployment()
    if scorer is None:
        _log.info(
            "%s not found in this image: the bench scorer is served by the bench-scorer "
            "service, not the worker",
            BENCH_SCORER["entrypoint"],
        )
```

Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/lint-imports.exe`
Expected: `Contracts: 10 kept, 0 broken.` If `only-the-scorer-reads-the-product` breaks on `testbench.serve_scorer`, do NOT add an `ignore_imports` line: the module imported something other than `prefect`/`testbench.score`; fix the import.

Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m mypy --strict selector_drift testbench && .claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m ruff check . && .claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m ruff format --check .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add testbench/serve_scorer.py tests/testbench/test_serve_scorer.py selector_drift/orchestrate/deployments.py
git commit -m "feat(testbench): bench-side entrypoint that serves the scorer

The worker image cannot serve the scorer, so it gets its own entrypoint; the worker
now logs that at info, not error."
```

---

### Task 4: The `bench-scorer` image and compose service

**Files:**
- Create: `docker/Dockerfile.bench-scorer`
- Modify: `docker-compose.yml` (new service after `testbench`)
- Create: `tests/testbench/test_scorer_service.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: role `sd_scorer` (Task 1); `SD_REDACTION_KEY` form (Task 2); `python -m testbench.serve_scorer` (Task 3).
- Produces: compose service `bench-scorer`.

- [ ] **Step 1: Write the failing tests**

Create `tests/testbench/test_scorer_service.py`:

```python
"""The bench-scorer compose service: it holds the redaction key and reads client heal bundles,
so its blast radius is pinned here rather than trusted to a comment."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SERVICES: dict[str, Any] = yaml.safe_load((REPO / "docker-compose.yml").read_text("utf-8"))[
    "services"
]
SCORER = SERVICES.get("bench-scorer", {})
FAIL_CLOSED = r"\$\{{{name}:\?\S.*\}}"


@pytest.mark.unit
def test_the_service_exists_and_runs_the_bench_side_entrypoint() -> None:
    assert SCORER, "docker-compose.yml has no bench-scorer service"
    assert SCORER["command"] == ["python", "-m", "testbench.serve_scorer"]
    assert SCORER["build"]["dockerfile"] == "docker/Dockerfile.bench-scorer"
    assert SCORER["restart"] == "unless-stopped"


@pytest.mark.unit
def test_it_is_hardened_like_every_other_service() -> None:
    assert SCORER["read_only"] is True
    assert SCORER["user"] == "10001:10001"
    assert SCORER["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in SCORER["security_opt"]
    assert not any("unconfined" in opt for opt in SCORER["security_opt"])
    assert "/tmp" in SCORER["tmpfs"]
    assert SCORER["mem_limit"] and SCORER["pids_limit"]
    assert "ports" not in SCORER and "cap_add" not in SCORER
    assert SCORER["init"] is True


@pytest.mark.unit
def test_it_connects_as_the_narrow_role_never_the_product_user() -> None:
    url = SCORER["environment"]["SD_DATABASE_URL"]
    assert url.startswith("postgresql+psycopg://sd_scorer:")
    assert "POSTGRES_USER" not in url and "POSTGRES_PASSWORD" not in url
    assert re.search(FAIL_CLOSED.format(name="SD_SCORER_PASSWORD"), url)


@pytest.mark.unit
def test_it_mounts_only_the_evidence_subpath_read_only() -> None:
    (mount,) = SCORER["volumes"]
    assert isinstance(mount, dict), "use the long form: the short form cannot carry a subpath"
    assert mount["source"] == "sd_data"
    assert mount["target"] == "/data/evidence"
    assert mount["read_only"] is True
    assert mount["volume"]["subpath"] == "evidence"
    assert SCORER["environment"]["SD_EVIDENCE_ROOT"] == "/data/evidence"


@pytest.mark.unit
def test_it_holds_the_same_redaction_key_as_the_worker_and_fails_closed() -> None:
    worker = SERVICES["worker"]["environment"]["SD_REDACTION_KEY"]
    scorer = SCORER["environment"]["SD_REDACTION_KEY"]
    assert scorer == worker
    assert re.fullmatch(FAIL_CLOSED.format(name="SD_REDACTION_KEY"), scorer)


@pytest.mark.unit
def test_it_has_no_browser_and_no_session_or_lake_paths() -> None:
    env = SCORER["environment"]
    assert not {"PLAYWRIGHT_BROWSERS_PATH", "SD_SESSION_DIR", "SD_LAKE_ROOT"} & set(env)
    assert "shm_size" not in SCORER


@pytest.mark.unit
def test_the_dockerfile_installs_no_browser_and_runs_non_root() -> None:
    text = (REPO / "docker" / "Dockerfile.bench-scorer").read_text("utf-8")
    assert "playwright install" not in text
    assert "COPY testbench" in text and "COPY selector_drift" in text
    assert "USER 10001:10001" in text
    assert 'CMD ["python", "-m", "testbench.serve_scorer"]' in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m pytest tests/testbench/test_scorer_service.py -q -p no:cacheprovider`
Expected: FAIL: `AssertionError: docker-compose.yml has no bench-scorer service` (and the Dockerfile test: `FileNotFoundError`).

- [ ] **Step 3: Write the Dockerfile**

Create `docker/Dockerfile.bench-scorer`:

```dockerfile
# syntax=docker/dockerfile:1.7
# The bench scorer's image: selector_drift + testbench, no browser. The product worker image
# (./Dockerfile) ships selector_drift only and runs Chromium; this one judges bench heals against
# the answer key and must never grow either of those. Build context is the repo root.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY selector_drift ./selector_drift
COPY testbench ./testbench
RUN uv sync --frozen --no-dev \
 && useradd --uid 10001 --home-dir /home/app --create-home app

USER 10001:10001
CMD ["python", "-m", "testbench.serve_scorer"]
```

- [ ] **Step 4: Add the compose service**

In `docker-compose.yml`, insert after the `testbench:` service (before the `caddy:` comment block):

```yaml
  # The bench scorer (Task 26C): judges each bench heal against the answer key and writes one
  # audit_log row per heal, which is where the false-accept count and the Task 29A alert come
  # from. Its own service because the worker image ships selector_drift/ only (bench spec 2), and
  # the portal image must not import the product. It holds SD_REDACTION_KEY (it matches the
  # redacted seed tag) and reads heal bundles, so: a Postgres role that can read five tables and
  # insert into audit_log and nothing else (alembic 0002), and ONLY the evidence subpath of
  # sd_data, read-only -- the rest of sd_data holds injected client sessions and the lake.
  # The subpath must exist before this starts (spec 7): the volume mount fails otherwise.
  bench-scorer:
    build:
      context: .
      dockerfile: docker/Dockerfile.bench-scorer
    restart: unless-stopped
    command: [ "python", "-m", "testbench.serve_scorer" ]
    init: true
    read_only: true
    user: "10001:10001"
    cap_drop: [ ALL ]
    security_opt:
      - no-new-privileges:true
    tmpfs: [ "/tmp" ]
    mem_limit: 512m
    pids_limit: 128
    volumes:
      - type: volume
        source: sd_data
        target: /data/evidence
        read_only: true
        volume:
          subpath: evidence
    environment:
      PREFECT_API_URL: http://prefect-server:4200/api
      PREFECT_HOME: /tmp/prefect                 # the rootfs is read-only
      SD_DATABASE_URL: postgresql+psycopg://sd_scorer:${SD_SCORER_PASSWORD:?set SD_SCORER_PASSWORD in .env -- see .env.example}@postgres:5432/${POSTGRES_DB:-selector_drift}
      SD_EVIDENCE_ROOT: /data/evidence
      SD_REDACTION_KEY: ${SD_REDACTION_KEY:?set SD_REDACTION_KEY in .env -- see .env.example}
      TZ: UTC
    depends_on:
      postgres: { condition: service_healthy }
      prefect-server: { condition: service_healthy }
```

- [ ] **Step 5: Document the deploy steps in `.env.example`**

Add after the `SD_REDACTION_KEY` line:

```
# bench-scorer (alembic 0002 creates the sd_scorer role with NO password). Generate a value,
# put it here, then set it on the role ONCE per database, after `alembic upgrade head`:
#   echo "ALTER ROLE sd_scorer PASSWORD :'pw'" | docker compose exec -T postgres \
#     psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-selector_drift}" -v pw="$SD_SCORER_PASSWORD"
# and create the evidence directory the read-only subpath mount needs:
#   docker compose run --rm --no-deps worker mkdir -p /data/evidence
SD_SCORER_PASSWORD=        # generate: python -c "import secrets; print(secrets.token_urlsafe(32))"
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m pytest tests/testbench/test_scorer_service.py tests/test_compose_fails_closed.py tests/orchestrate/test_deployments.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 7: Prove it with Docker (not just the YAML)**

Run, with throwaway values (never real secrets):

```bash
export POSTGRES_PASSWORD=x SD_SCORER_PASSWORD=y SD_REDACTION_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
docker compose config -q && echo "compose config valid"
docker build -f docker/Dockerfile.bench-scorer -t sd-bench-scorer:plan-check .
docker run --rm --entrypoint python sd-bench-scorer:plan-check -c "import testbench.score, testbench.serve_scorer; print('imports ok')"
docker run --rm --entrypoint sh sd-bench-scorer:plan-check -c "ls /opt/pw-browsers 2>&1 | head -1; id -u"
```

Expected: `compose config valid`; build succeeds; `imports ok`; then `ls: cannot access '/opt/pw-browsers'` (no browsers) and `10001`. If `docker compose config` rejects `subpath`, the Compose version is older than 2.26: report it as a deployment requirement, do not fall back to mounting the whole `sd_data`. If the import fails on `lxml`, the testbench dependency group is missing from `uv sync`: fix the Dockerfile (`--group testbench`), not the code. If Docker is not running, say so in the report and mark this step unverified.

- [ ] **Step 8: Commit**

```bash
git add docker/Dockerfile.bench-scorer docker-compose.yml tests/testbench/test_scorer_service.py .env.example
git commit -m "feat(compose): bench-scorer service, hardened, narrow role, evidence read-only"
```

---

### Task 5: Reviews and final verification

**Files:** none created; findings are fixed in the files above, each in its own commit.

- [ ] **Step 1: Run the reviewers CLAUDE.md requires for the paths touched**

Dispatch, each against `git diff <base>..HEAD` of this plan's commits:
- `ecc:database-reviewer` — migration 0002 and the grants (cluster-wide role creation, race tolerance, sequence grant, downgrade).
- `ecc:security-reviewer` — `docker-compose.yml`, `docker/Dockerfile.bench-scorer`, the second holder of `SD_REDACTION_KEY`, the evidence mount, `.env.example`.
- `ecc:python-reviewer` — `testbench/serve_scorer.py` and the new tests.

Fix every Critical and Important finding with a test first; record Minor ones.

- [ ] **Step 2: Whole-branch verification**

Run each and read the output:
- `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m pytest tests/testbench tests/db tests/test_compose_fails_closed.py tests/orchestrate/test_deployments.py -q -p no:cacheprovider` — expect all pass (integration ones need the `*_test` database).
- `.claude/worktrees/selector-drift-impl/.venv/Scripts/lint-imports.exe` — `10 kept, 0 broken`.
- `.claude/worktrees/selector-drift-impl/.venv/Scripts/python.exe -m mypy --strict selector_drift testbench`, `ruff check .`, `ruff format --check .` — clean.
- Confirm in the report which steps ran against real Postgres and real Docker and which did not.

- [ ] **Step 3: Report**

State plainly: what is verified, what is not (the first real deploy on the VM is not, and cannot be from here), and the manual deploy steps from spec section 7.
