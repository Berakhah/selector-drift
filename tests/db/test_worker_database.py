"""Parallel-run isolation: under `pytest -n auto` every xdist worker gets its own database.

The orchestrate `db` fixture TRUNCATEs the run-history tables before each test. With one shared
`selector_drift_test` database, two workers running DB tests at once deleted each other's rows
mid-test (ObjectDeletedError on Heartbeat, MultipleResultsFound / NoResultFound) -- green
serially, red in parallel. `tests/conftest.py` now derives a per-worker database name; these are
unit tests for that pure helper. The derived name must keep the `_test` suffix so the fail-closed
guard (`require_test_database_url`) still applies to it unchanged.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from tests.conftest import NotATestDatabaseError, require_test_database_url, worker_database_url

BASE = "postgresql+psycopg://selector_drift:x@127.0.0.1:5432/selector_drift_test"


@pytest.mark.unit
@pytest.mark.parametrize("worker", ["", "master"])
def test_a_serial_run_keeps_the_configured_database(worker: str) -> None:
    assert worker_database_url(BASE, worker) == BASE


@pytest.mark.unit
def test_each_worker_gets_its_own_database_that_still_ends_in_test() -> None:
    gw0 = make_url(worker_database_url(BASE, "gw0"))
    gw1 = make_url(worker_database_url(BASE, "gw1"))
    assert gw0.database == "selector_drift_gw0_test"
    assert gw1.database == "selector_drift_gw1_test"
    require_test_database_url(gw0.render_as_string(hide_password=False))


@pytest.mark.unit
def test_only_the_database_name_changes() -> None:
    derived = make_url(worker_database_url(BASE, "gw3"))
    original = make_url(BASE)
    assert derived.set(database=original.database) == original
    assert derived.password == "x"


@pytest.mark.unit
def test_a_non_test_base_database_is_refused_before_any_name_is_derived() -> None:
    with pytest.raises(NotATestDatabaseError):
        worker_database_url(
            "postgresql+psycopg://selector_drift:x@127.0.0.1:5432/selector_drift", "gw0"
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "database", ['weird"; DROP DATABASE evil; --_test', "has space_test", "Upper_test"]
)
def test_a_base_name_outside_the_identifier_allowlist_is_refused(database: str) -> None:
    """Review round 1, Critical: the base name is interpolated into CREATE/DROP DATABASE too, and
    ending in `_test` says nothing about the rest of it."""
    url = make_url(BASE).set(database=database).render_as_string(hide_password=False)
    with pytest.raises(ValueError):
        worker_database_url(url, "gw0")


@pytest.mark.unit
@pytest.mark.parametrize("worker", ["gw0; DROP", "gw-1", "GW0", "gw"])
def test_an_unexpected_worker_id_is_refused(worker: str) -> None:
    """The name is interpolated into CREATE/DROP DATABASE; only xdist's `gw<N>` shape is let in."""
    with pytest.raises(ValueError):
        worker_database_url(BASE, worker)
