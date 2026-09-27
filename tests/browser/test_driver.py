import inspect
import os
import subprocess
import sys
import textwrap
import time

import pytest

from selector_drift import browser
from selector_drift.browser import ratelimit


@pytest.mark.unit
def test_no_public_function_accepts_a_bare_url():
    """Weak by construction -- it only catches a parameter *named* `url` (M19). Kept as a
    typo guard; the real control is test_navigation_off_the_authorised_origin_is_aborted."""
    for name, fn in inspect.getmembers(browser.driver, inspect.isfunction):
        if name.startswith("_"):
            continue
        hints = inspect.get_annotations(fn, eval_str=True)
        assert str not in {hints.get(k) for k in ("url", "href", "path", "target_url")}, name


@pytest.mark.unit
def test_the_registry_knows_exactly_the_three_day_one_targets():
    from selector_drift.browser import registry

    assert set(registry._URLS) == {"testbench", "agent-perimeter", "ledger-sense"}
    with pytest.raises(registry.UnknownTarget):
        registry.url_for("anything-else")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_open_loads_a_page(authorised_testbench):
    async with browser.driver.open(authorised_testbench) as session:
        assert "<html" in (await session.page.content()).lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_navigation_off_the_authorised_origin_is_aborted(
    authorised_testbench, evil_redirect, bench
):
    """R8/H2. This is the test the scope gate is *claimed* to have. Without it the gate
    authorises the first navigation and nothing after it."""
    async with browser.driver.open(authorised_testbench) as session:
        with pytest.raises(browser.OffScopeNavigation):
            await session.page.click("#leaves-the-portal")
        assert any(u.startswith(bench.off_origin) for u in session.blocked_urls)
    # The redirect hop is invisible to a Playwright route handler; prove the request never
    # reached the wire, not merely that the driver says it refused it.
    assert (f"127.0.0.1:{bench.port}", "/evil") in bench.seen
    assert not [s for s in bench.seen if s[0].startswith("localhost")], bench.seen


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_subresource_from_another_origin_is_aborted(authorised_testbench, bench):
    async with browser.driver.open(authorised_testbench) as session:
        await session.page.goto_step({"step": "goto", "path": "/results"})
        assert all(
            r.startswith(tuple(authorised_testbench.origins)) for r in session.requested_urls
        )
        assert session.blocked_urls, "nothing was blocked -- the assertion above is vacuous"
    assert not [s for s in bench.seen if s[0].startswith("localhost")], bench.seen


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["http://localhost:1/", "//localhost:1/", "https://example.invalid/", "/\\example.invalid/"],
)
async def test_a_goto_step_off_the_authorised_origin_is_refused_before_any_request(
    authorised_testbench, path
):
    async with browser.driver.open(authorised_testbench) as session:
        before = (list(session.requested_urls), list(session.blocked_urls))
        with pytest.raises(browser.OffScopeNavigation):
            await session.page.goto_step({"step": "goto", "path": path})
        assert (session.requested_urls, session.blocked_urls) == before


@pytest.mark.integration
def test_the_rate_limit_comes_from_the_target_and_not_from_config(
    authorised_testbench, monkeypatch
):
    """H4, design 4.3: raising it means editing the scope record, which is the document
    you would point at if anyone ever asked why you accessed a system."""
    monkeypatch.setenv("SD_RATE_LIMIT_PER_SECONDS", "0")
    assert ratelimit.window_for(authorised_testbench) == 30


_SECOND_PROCESS = textwrap.dedent(
    """
    import time
    from datetime import UTC, datetime, timedelta
    from selector_drift.browser import ratelimit
    from selector_drift.scope.gate import authorise
    from selector_drift.scope.record import ScopeRecord

    today = datetime.now(UTC).date()
    target = authorise(
        ScopeRecord(
            target="testbench",
            basis="self-owned",
            authorising_party="selector-drift test suite",
            authorised_on=today - timedelta(days=1),
            expires_on=today + timedelta(days=365),
            evidence_uri=None,
            evidence_sha256=None,
            origins=["http://127.0.0.1:1"],
            rate_limit={"requests": 1, "per_seconds": 30},
            user_agent_contact="https://example.invalid/contact",
            first_heal_policy="required",
            legal_review="self_drafted",
        ),
        today=today,
    )
    t0 = time.monotonic()
    ratelimit.acquire(target)
    print(time.monotonic() - t0)
    """
)


@pytest.mark.integration
def test_the_limiter_is_persistent_across_processes(authorised_testbench):
    """Ten runs starting at once must not each get their own fresh bucket."""
    ratelimit.acquire(authorised_testbench)
    t0 = time.monotonic()
    out = subprocess.run(
        [sys.executable, "-c", _SECOND_PROCESS],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert time.monotonic() - t0 >= 29
    # The wait happened inside the second process's acquire(), not in interpreter start-up
    # (which legitimately spends part of the 30 s window, so this cannot also be >= 29).
    assert float(out.stdout.strip().splitlines()[-1]) >= 20, out.stdout
