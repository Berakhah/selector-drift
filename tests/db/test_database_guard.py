"""Fix-wave item 1: integration tests must refuse to run against any database whose name does
not end in `_test`, so a test run can never write into the development or log database. The
check itself lives in `tests/conftest.py` (one shared place, wired into an autouse fixture that
applies to every `@pytest.mark.integration` test) -- these are unit tests for the pure helper
function that fixture calls."""

from __future__ import annotations

import pytest

from tests.conftest import NotATestDatabaseError, require_test_database_url


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://selector_drift:x@127.0.0.1:5432/selector_drift",
        "postgresql+psycopg://selector_drift:x@127.0.0.1:5432/selector_drift_log",
        "postgresql+psycopg://selector_drift:x@127.0.0.1:5432/testing",
    ],
)
def test_rejects_a_database_not_named_with_the_test_suffix(url: str) -> None:
    with pytest.raises(NotATestDatabaseError):
        require_test_database_url(url)


@pytest.mark.unit
def test_accepts_a_database_named_with_the_test_suffix() -> None:
    require_test_database_url(
        "postgresql+psycopg://selector_drift:x@127.0.0.1:5432/selector_drift_test"
    )


@pytest.mark.unit
def test_rejects_an_empty_url() -> None:
    with pytest.raises(NotATestDatabaseError):
        require_test_database_url("")
