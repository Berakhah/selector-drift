"""Never-rule 7 against a 307: the sign-in's one POST may not be replayed.

The write window lets exactly one POST through -- the sign-in. A portal that answers it with a
307/308 makes Chromium replay the same POST at the `Location`. Playwright's route handler never
sees that hop, so the replay was only recorded after the fact; the portal had already received
a second write. These run against real Chromium and a server that counts what it received.

Callers: none (pytest collects tests/browser/test_*.py). Instruction: "look at the redirect hole".
"""

from __future__ import annotations

import contextlib

import pytest

from selector_drift.browser import driver
from selector_drift.browser.allowlist import WriteBlocked
from selector_drift.scope.record import AuthorisedTarget
from tests.browser.conftest import Bench


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_307_from_the_sign_in_post_is_never_replayed_at_the_portal(
    bench: Bench, authorised_testbench: AuthorisedTarget
) -> None:
    bench.posts.clear()
    async with driver.open(authorised_testbench) as session:
        await session.page.goto_step({"step": "goto", "path": "/signin"})
        async with session.page.submitting_forms():
            with contextlib.suppress(WriteBlocked):  # the run stops; the server is what we check
                await session.page.click_role("button", "Sign in")
    assert bench.posts == ["/signin"]  # the sign-in itself, and no replay at /landed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_307_from_the_sign_in_post_stops_the_run(
    bench: Bench, authorised_testbench: AuthorisedTarget
) -> None:
    async with driver.open(authorised_testbench) as session:
        await session.page.goto_step({"step": "goto", "path": "/signin"})
        with pytest.raises(WriteBlocked, match="/landed"):
            async with session.page.submitting_forms():
                await session.page.click_role("button", "Sign in")
