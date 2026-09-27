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
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import make_url


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


@pytest.fixture(autouse=True)
def _guard_integration_database_name(request: pytest.FixtureRequest) -> None:
    """Runs before every test in the suite; only integration-marked tests pay for it."""
    if request.node.get_closest_marker("integration") is None:
        return
    require_test_database_url(os.environ.get("SD_DATABASE_URL", ""))
