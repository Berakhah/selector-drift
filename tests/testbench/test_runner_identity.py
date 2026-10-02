from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading

import pytest

from selector_drift.browser import driver
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import Basis, ScopeRecord

CONTACT = "https://example.invalid/selector-drift-contact"


@pytest.mark.e2e
def test_the_runner_identifies_itself_to_the_bench(
    bench_server: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Brief §3.3, checked from the other end: every request the product's driver makes reaches
    the bench carrying the scope record's contact URL. Task 4's driver reads the URL through
    `registry.url_for` at call time, so patching the module attribute points it at this server.

    A plain sync test (T-I1, fix round 1), not `@pytest.mark.asyncio`: the async body runs via
    `asyncio.run()` inside a fresh OS thread, joined here. Playwright's sync API -- used by this
    same directory's other e2e tests (`test_portal_e2e.py`, `test_served_equals_tested.py`) --
    runs its own `asyncio` event loop inside a greenlet on the *main* thread for the lifetime of
    its session-scoped `browser` fixture
    (`.venv/.../playwright/sync_api/_context_manager.py:run_until_complete`, never unwound until
    teardown), which leaves `asyncio.events._get_running_loop()` non-None on that thread. Any
    later `asyncio.Runner`-based test collected afterward in the same pytest session then fails
    with `RuntimeError: Runner.run() cannot be called from a running event loop` -- reproduced
    with a minimal two-test session before this fix. A dedicated thread gives this test's loop
    its own, unrelated thread-local state, so `uv run pytest -q -m e2e tests/testbench` passes
    as one combined session regardless of collection order."""
    today = dt.datetime.now(dt.UTC).date()
    record = ScopeRecord(
        target="testbench",
        basis=Basis.SELF_OWNED,
        authorising_party="test",
        authorised_on=today - dt.timedelta(days=1),
        expires_on=today + dt.timedelta(days=30),
        evidence_uri=None,
        evidence_sha256=None,
        origins=[bench_server],
        rate_limit={"requests": 1, "per_seconds": 1},
        user_agent_contact=CONTACT,
        first_heal_policy="auto_promote",
        legal_review="self_drafted",
    )
    target = authorise(record, today=today)
    monkeypatch.setattr("selector_drift.browser.registry.url_for", lambda _name: bench_server)
    caplog.set_level(logging.INFO, logger="testbench.access")

    async def visit() -> None:
        async with driver.open(target) as session:
            await session.page.content()

    error: BaseException | None = None

    def run_in_its_own_loop() -> None:
        nonlocal error
        try:
            asyncio.run(visit())
        except BaseException as exc:  # re-raised on the test's own thread below
            error = exc

    thread = threading.Thread(target=run_in_its_own_loop)
    thread.start()
    thread.join()
    if error is not None:
        raise error

    agents = [getattr(r, "user_agent", "") for r in caplog.records if r.name == "testbench.access"]
    assert agents and all(CONTACT in agent for agent in agents)
