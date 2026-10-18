"""Shared fixtures for the whole suite.

Fix-wave item 1: an integration test that obtains an engine or session against
`SD_DATABASE_URL` must never be able to touch the development or log database -- a broken
selector throws and gets fixed, but a test suite quietly writing rows into `selector_drift`
(or worse, a shared log database) is exactly the kind of silent corruption this project treats
as the worse failure mode (CLAUDE.md, "Never" 1 and 5, applied here to our own database rather
than a client's). The guard is a single autouse fixture here, not copied into `tests/db/` and
`tests/browser/conftest.py` separately, so there is exactly one place to update if the rule
ever changes.

Unit tests (`@pytest.mark.unit`) never touch Postgres and are exempt -- they must not need a
database at all.

Parallel runs (`-n auto`): each xdist worker gets its own freshly migrated database,
`<base>_gw<N>_test`, provisioned in `pytest_configure` before anything imports
`selector_drift.db.session` (whose engine is built at import time). One shared database let the
orchestrate `db` fixture's TRUNCATE in one worker delete rows another worker's test was using.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# Prefect's background log shipper POSTs to its API (`/api/logs/`) from a worker thread that
# outlives the flow test that started it; on the same xdist worker a later `httpx_mock` test
# then sees that request as unexpected and fails. No test reads logs back from the API, so ship
# none. Set here, before anything imports prefect, so every worker and subprocess inherits it.
os.environ["PREFECT_LOGGING_TO_API_ENABLED"] = "false"

REPO = Path(__file__).resolve().parents[1]
_XDIST_WORKER = re.compile(r"gw\d+")
_DATABASE_NAME = re.compile(r"[a-z0-9_]+")


class NotATestDatabaseError(RuntimeError):
    """Raised when `SD_DATABASE_URL` does not name a database reserved for tests."""


def require_test_database_url(url: str) -> None:
    """Fail closed unless `url` names a database ending in `_test`.

    Deliberately strict about the empty string too: an unset `SD_DATABASE_URL` must not fall
    back to `db.session`'s password-less development default and run integration tests
    against it.
    """
    if not url:
        raise NotATestDatabaseError(
            "SD_DATABASE_URL is not set. Integration tests must point at a dedicated "
            "database whose name ends in '_test', e.g. "
            "postgresql+psycopg://selector_drift:<password>@127.0.0.1:5432/selector_drift_test "
            "-- see .env.example."
        )
    database = make_url(url).database or ""
    if not database.endswith("_test"):
        raise NotATestDatabaseError(
            f"Refusing to run integration tests against database {database!r}. "
            "SD_DATABASE_URL must name a database ending in '_test' so a test run can never "
            "write into the development or log database -- see .env.example."
        )


class DatabaseUnreachableError(RuntimeError):
    """Raised when the database a test needs does not answer within the probe's timeout."""


# url -> failure message, or None once it answered. Failures are cached too: re-probing per test
# would put the timeout back in front of every one of them.
_REACHABILITY: dict[str, str | None] = {}


def require_database_reachable(url: str, *, timeout: float = 3.0) -> None:
    """Fail fast, once per URL, unless Postgres answers within `timeout` seconds.

    The engine in `db.session` sets no connect timeout, and against an unreachable
    `localhost:5432` libpq took ~130 s to give up -- per test, so a run looked hung. This fails
    instead of skipping: a CI run with its database missing must not go green. The message names
    host, port and database only, never the credentials.
    """
    if url not in _REACHABILITY:
        import psycopg

        parsed = make_url(url)
        conninfo = parsed.set(drivername="postgresql").render_as_string(hide_password=False)
        try:
            psycopg.connect(conninfo, connect_timeout=max(1, round(timeout))).close()
        except psycopg.Error:
            _REACHABILITY[url] = (
                f"Postgres at {parsed.host}:{parsed.port} (database {parsed.database!r}) did not "
                f"answer within {timeout:g}s. Start it (docker compose up -d postgres), point "
                "SD_DATABASE_URL at it, or deselect the tests that need it "
                "(-m 'not integration and not e2e')."
            )
        else:
            _REACHABILITY[url] = None
    failure = _REACHABILITY[url]
    if failure is not None:
        raise DatabaseUnreachableError(failure)


def require_usable_test_database(url: str) -> None:
    """The one gate for a test that touches Postgres: the name guard first (so the development
    or log database is never even connected to), then the reachability probe."""
    require_test_database_url(url)
    require_database_reachable(url)


def worker_database_url(url: str, worker: str) -> str:
    """The database URL one xdist worker should use: `<base>_gw<N>_test` in place of
    `<base>_test`, everything else unchanged. A serial run (`worker` empty or `"master"`) keeps
    `url` as-is. The base must already pass the `_test` guard, and the worker id must be xdist's
    `gw<N>` shape, and the base must be a plain lower-case identifier -- the derived name is
    interpolated into CREATE/DROP DATABASE, and ending in `_test` says nothing about the rest."""
    require_test_database_url(url)
    if worker in ("", "master"):
        return url
    if not _XDIST_WORKER.fullmatch(worker):
        raise ValueError(f"Unexpected xdist worker id {worker!r}; expected 'gw<N>'.")
    parsed = make_url(url)
    database = parsed.database or ""
    if not _DATABASE_NAME.fullmatch(database):
        raise ValueError(f"Database name {database!r} must match {_DATABASE_NAME.pattern!r}.")
    base = database.removesuffix("_test")
    return parsed.set(database=f"{base}_{worker}_test").render_as_string(hide_password=False)


def _provision_worker_database(url: str) -> None:
    """Drop and recreate the worker's database, then migrate it to head. Recreated every run so
    no state survives from a previous one."""
    parsed = make_url(url)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        admin.dispose()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env={**os.environ, "SD_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed for worker database {parsed.database!r}:\n{result.stderr}"
        )


def pytest_configure(config: pytest.Config) -> None:
    """On an xdist worker, point `SD_DATABASE_URL` at that worker's own migrated database.

    A URL that is unset or not a `_test` database is left alone: unit-only runs need no
    database, and the per-test guard below still refuses every integration test against it.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    url = os.environ.get("SD_DATABASE_URL", "")
    if not worker or not url:
        return
    try:
        require_test_database_url(url)
    except NotATestDatabaseError:
        return
    if "selector_drift.db.session" in sys.modules:
        raise RuntimeError(
            "selector_drift.db.session was imported before the worker database was provisioned; "
            "its engine would still point at the shared test database."
        )
    derived = worker_database_url(url, worker)
    _provision_worker_database(derived)
    os.environ["SD_DATABASE_URL"] = derived
    # Review round 1, Important: `get_settings` is lru_cached. Anything that reached it before
    # this hook would keep handing out the shared URL, silently undoing the isolation.
    from selector_drift.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _guard_integration_database_name(request: pytest.FixtureRequest) -> None:
    """Runs before every test in the suite; only integration-marked tests pay for it."""
    if request.node.get_closest_marker("integration") is None:
        return
    require_usable_test_database(os.environ.get("SD_DATABASE_URL", ""))
