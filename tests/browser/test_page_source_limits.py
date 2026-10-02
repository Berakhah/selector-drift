"""Fix round 1 (S-I2): the live snapshot is bounded in time and in size, and refuses rather
than truncating. A fake page, so unit: no browser."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

import pytest

from selector_drift.browser import page_source
from selector_drift.browser.page_source import (
    MAX_SNAPSHOT_NODES,
    SNAPSHOT_TIMEOUT_S,
    LivePageSource,
    SnapshotTooLarge,
)


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    """asyncio.run in a fresh thread: safe whatever loop pytest-playwright left on this one."""
    out: list[Any] = []
    err: list[BaseException] = []

    def go() -> None:
        try:
            out.append(asyncio.run(coro))
        except BaseException as exc:  # re-raised on the test's own thread below
            err.append(exc)

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "the snapshot did not return"
    if err:
        raise err[0]
    return out[0]


@dataclass
class FakePage:
    result: dict[str, Any]
    delay: float = 0.0
    args: list[Any] = field(default_factory=list)

    async def evaluate(self, script: str, arg: Any) -> dict[str, Any]:
        self.args.append(arg)
        await asyncio.sleep(self.delay)
        return self.result

    def locator(self, selector: str) -> Any:
        raise AssertionError("a refused snapshot must not go on to read the AX tree")


@pytest.mark.unit
def test_the_limits_are_the_ruled_values() -> None:
    assert (SNAPSHOT_TIMEOUT_S, MAX_SNAPSHOT_NODES) == (30, 50_000)


@pytest.mark.unit
def test_a_page_past_the_ceiling_is_refused_not_truncated() -> None:
    page = FakePage({"refused": "too_large", "count": MAX_SNAPSHOT_NODES + 1})
    with pytest.raises(SnapshotTooLarge, match=str(MAX_SNAPSHOT_NODES + 1)):
        _run(LivePageSource.from_playwright(page))  # type: ignore[arg-type]
    assert page.args[0]["max"] == MAX_SNAPSHOT_NODES


@pytest.mark.unit
def test_a_snapshot_that_overruns_the_timeout_is_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(page_source, "SNAPSHOT_TIMEOUT_S", 0.05)
    page = FakePage({"html": "<html></html>", "rendered": [], "boxed": []}, delay=5)
    with pytest.raises(TimeoutError):
        _run(LivePageSource.from_playwright(page))  # type: ignore[arg-type]
