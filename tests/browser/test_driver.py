import datetime as dt
import inspect
import os
import subprocess
import sys
import textwrap
import time

import pytest

from selector_drift import browser
from selector_drift.browser import ratelimit
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import Basis, ScopeRecord


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
async def test_an_off_origin_iframe_is_aborted_but_is_not_a_scope_violation(
    authorised_testbench, bench
):
    """An iframe's navigation is a sub-frame navigation: it is aborted and recorded like any
    subresource, but the page the run is on is still the authorised one, so the driver must
    not raise OffScopeNavigation -- neither on the goto that loads it nor afterwards."""
    widget = f"{bench.off_origin}/widget"
    async with browser.driver.open(authorised_testbench) as session:
        await session.page.goto_step({"step": "goto", "path": "/framed"})
        assert "host page" in await session.page.content()
        assert widget in session.blocked_urls, session.blocked_urls
    assert (f"127.0.0.1:{bench.port}", "/framed") in bench.seen
    assert not [s for s in bench.seen if s[0].startswith("localhost")], bench.seen


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_landing_page_with_an_off_origin_iframe_opens(
    authorised_testbench, bench, monkeypatch
):
    monkeypatch.setattr(
        "selector_drift.browser.registry.url_for", lambda _target: f"{bench.base_url}framed"
    )
    async with browser.driver.open(authorised_testbench) as session:
        assert "host page" in await session.page.content()
        assert f"{bench.off_origin}/widget" in session.blocked_urls, session.blocked_urls
    assert not [s for s in bench.seen if s[0].startswith("localhost")], bench.seen


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_refuses_an_expired_target_before_launching_anything(monkeypatch):
    """A target authorised on a date in the past and held past its expiry (a long-lived
    orchestrator) must be refused by the driver itself. Nothing is launched: the registry
    lookup and Playwright are both booby-trapped."""
    past = dt.date(2026, 1, 10)
    target = authorise(
        ScopeRecord(
            target="driver-fixture",
            basis=Basis.SELF_OWNED,
            authorising_party="selector-drift test suite",
            authorised_on=past - dt.timedelta(days=5),
            expires_on=past,
            evidence_uri=None,
            evidence_sha256=None,
            origins=["http://127.0.0.1:1"],
            rate_limit={"requests": 1, "per_seconds": 30},
            user_agent_contact="https://example.invalid/contact",
            first_heal_policy="required",
            legal_review="self_drafted",
        ),
        today=past,
    )

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("open() went past the expiry check")

    monkeypatch.setattr("selector_drift.browser.registry.url_for", _boom)
    monkeypatch.setattr("selector_drift.browser.driver.async_playwright", _boom)
    with pytest.raises(ScopeRefusal, match="expired"):
        async with browser.driver.open(target):
            pass


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
@pytest.mark.asyncio
async def test_a_goto_step_that_redirects_off_origin_raises_off_scope(authorised_testbench, bench):
    """I-2. The redirect hop fails at the dead proxy with a generic net error; it must surface
    as OffScopeNavigation so an orchestrator refuses rather than retries."""
    async with browser.driver.open(authorised_testbench) as session:
        with pytest.raises(browser.OffScopeNavigation):
            await session.page.goto_step({"step": "goto", "path": "/evil"})
    assert not [s for s in bench.seen if s[0].startswith("localhost")], bench.seen


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_landing_page_that_redirects_off_origin_raises_off_scope(
    authorised_testbench, evil_redirect, bench, monkeypatch
):
    monkeypatch.setattr("selector_drift.browser.registry.url_for", lambda _target: evil_redirect)
    with pytest.raises(browser.OffScopeNavigation):
        async with browser.driver.open(authorised_testbench):
            pass
    assert not [s for s in bench.seen if s[0].startswith("localhost")], bench.seen


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
            target="driver-fixture",
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
