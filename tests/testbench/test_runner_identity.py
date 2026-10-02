from __future__ import annotations

import datetime as dt
import logging

import pytest

from selector_drift.browser import driver
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import Basis, ScopeRecord

CONTACT = "https://example.invalid/selector-drift-contact"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_the_runner_identifies_itself_to_the_bench(
    bench_server: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Brief §3.3, checked from the other end: every request the product's driver makes reaches
    the bench carrying the scope record's contact URL. Task 4's driver reads the URL through
    `registry.url_for` at call time, so patching the module attribute points it at this server."""
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
    async with driver.open(target) as session:
        await session.page.content()
    agents = [getattr(r, "user_agent", "") for r in caplog.records if r.name == "testbench.access"]
    assert agents and all(CONTACT in agent for agent in agents)
