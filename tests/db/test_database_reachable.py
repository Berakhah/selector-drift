"""A browser or integration test needs Postgres; when it is not there the suite must fail fast
with a message, not sit in libpq for minutes per test (observed: ~130 s to fail against
127.0.0.1:5432, once per e2e test -- the run looked hung). The probe lives in `tests/conftest.py`;
these are unit tests for it, using a local socket in place of Postgres."""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from tests.conftest import DatabaseUnreachableError, require_database_reachable

PASSWORD = "S3CRET-probe-canary"


@contextmanager
def _stalled_server() -> Iterator[int]:
    """Accepts TCP connections (the kernel does, via the backlog) and never answers."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    try:
        yield s.getsockname()[1]
    finally:
        s.close()


def _url(port: int) -> str:
    return f"postgresql+psycopg://selector_drift:{PASSWORD}@127.0.0.1:{port}/selector_drift_test"


@pytest.mark.unit
def test_a_server_that_never_answers_fails_within_the_timeout() -> None:
    with _stalled_server() as port:
        started = time.monotonic()
        with pytest.raises(DatabaseUnreachableError):
            require_database_reachable(_url(port), timeout=1)
        assert time.monotonic() - started < 10


@pytest.mark.unit
def test_a_refused_port_fails_and_the_message_names_the_host_but_not_the_password() -> None:
    with _stalled_server() as port:
        pass  # closed on exit: nothing listens on `port` now
    with pytest.raises(DatabaseUnreachableError) as caught:
        require_database_reachable(_url(port), timeout=1)
    message = str(caught.value)
    assert f"127.0.0.1:{port}" in message
    assert PASSWORD not in message
