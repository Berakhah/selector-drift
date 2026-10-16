"""A local provider blackout (Task 26B), standing in for bok_core.testing.degraded, which the
installed bok-core slice does not ship yet -- swap the import when it does.

Inside `blackout()` every outbound network path is blocked: socket connect / connect_ex,
socket.create_connection and DNS (getaddrinfo). Every HTTP client and every model-provider SDK
ends in one of these, so no provider can be reached. Each attempt is RECORDED, then refused with
BlackoutViolation; `assert_blackout_complete()` fails if anything was attempted, so code that
swallows the exception (a retrying client, a broad `except`) still cannot pass.

Rung 5 is deliberately not patched: in v1 it records an escalation case and raises
EscalateToHuman with zero model calls (design 5.3). A model call added to it later would go
through a socket and be caught here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from unittest import mock

_TARGETS = (
    "socket.socket.connect",
    "socket.socket.connect_ex",
    "socket.create_connection",
    "socket.getaddrinfo",
)
_attempts: list[str] = []
_depth: list[None] = []


class BlackoutViolation(RuntimeError):
    """Something tried to reach the network -- and so a provider -- under blackout."""


def _refuse(target: str) -> Callable[..., object]:
    def blocked(*args: object, **kwargs: object) -> object:
        _attempts.append(target)
        raise BlackoutViolation(f"{target} attempted under blackout")

    return blocked


@contextmanager
def blackout() -> Iterator[list[str]]:
    """Block every outbound connection; yield the live attempt log."""
    _attempts.clear()
    with ExitStack() as stack:
        for target in _TARGETS:
            stack.enter_context(mock.patch(target, _refuse(target)))
        _depth.append(None)
        try:
            yield _attempts
        finally:
            _depth.pop()


def assert_blackout_complete() -> None:
    """Fail if anything tried to reach the network during the current blackout."""
    assert _depth, "assert_blackout_complete() must be called inside blackout()"
    assert not _attempts, f"network/provider calls attempted under blackout: {_attempts}"
