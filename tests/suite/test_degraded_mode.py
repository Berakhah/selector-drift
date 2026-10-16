"""Task 26B: the degraded-mode floor (00 R3; INTERFACE-CONTRACT section 5).

Floor and unit: a MAJORITY (> half) of the benign mutation classes heal -- resolved by rungs 1-4
and accepted by the gate -- with every model provider and all outbound network disabled. v1 makes
zero model calls, so this is a regression guard against a model dependency creeping in.
"""

import contextlib
import socket
import urllib.request

import pytest

from selector_drift.validate.gate import ValidatedExtraction
from tests.suite.blackout import BlackoutViolation, assert_blackout_complete, blackout
from tests.suite.conftest import ALL_BENIGN_MUTATION_CLASSES, run_pipeline_on


@pytest.mark.unit
def test_degraded_mode_still_produces_findings(bench_evidence, history, warm_cache):
    with blackout():

        def heals_under_blackout(m) -> bool:
            result = run_pipeline_on(bench_evidence[m.name], history=history, cache=warm_cache())
            return isinstance(result, ValidatedExtraction)

        healed = [m.name for m in ALL_BENIGN_MUTATION_CLASSES if heals_under_blackout(m)]
        # The floor is a majority; the bench holds 8/8, so a drop below that is a regression to see.
        assert len(healed) > len(ALL_BENIGN_MUTATION_CLASSES) / 2, healed
        assert healed == [m.name for m in ALL_BENIGN_MUTATION_CLASSES], healed
        assert_blackout_complete()


@pytest.mark.unit
@pytest.mark.parametrize(
    "attempt",
    [
        lambda: socket.create_connection(("127.0.0.1", 9), timeout=0.1),
        lambda: socket.socket().connect(("127.0.0.1", 9)),
        lambda: socket.socket().connect_ex(("127.0.0.1", 9)),
        lambda: socket.getaddrinfo("example.invalid", 443),
        lambda: urllib.request.urlopen("http://example.invalid/", timeout=0.1),
    ],
    ids=["create_connection", "connect", "connect_ex", "getaddrinfo", "urllib"],
)
def test_the_blackout_trips_on_any_connection_attempt(attempt):
    # Without this the degraded test could pass vacuously with a blackout that blocks nothing.
    with blackout() as attempts:
        with pytest.raises(BlackoutViolation):
            attempt()
        assert attempts
        with pytest.raises(AssertionError, match="attempted under blackout"):
            assert_blackout_complete()


@pytest.mark.unit
def test_a_swallowed_attempt_still_fails_the_blackout():
    # A retrying client or a broad `except` must not hide the call.
    with blackout():
        with contextlib.suppress(Exception):
            socket.create_connection(("127.0.0.1", 9), timeout=0.1)
        with pytest.raises(AssertionError):
            assert_blackout_complete()


@pytest.mark.unit
def test_the_blackout_is_lifted_afterwards_and_starts_clean():
    with blackout(), pytest.raises(BlackoutViolation):
        socket.getaddrinfo("example.invalid", 443)
    assert socket.getaddrinfo.__module__ == "socket"  # the real function is back
    with blackout() as attempts:
        assert attempts == []
        assert_blackout_complete()
