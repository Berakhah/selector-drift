"""The browser driver: the only way the product touches a web page.

It cannot be handed a URL. `open()` takes an `AuthorisedTarget` -- which only the scope gate can
mint -- and looks the URL up in `registry`; every later navigation is a *path* resolved against
that URL and checked against the target's origins before any request is made. Every request
the page makes after that is held to the same origins by `allowlist` (R8/H2), and every
top-level navigation the driver initiates first takes a token from the persistent,
cross-process `ratelimit` (R8/H4).

One browser and one context per run, torn down in `__aexit__` even on error.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urljoin

from bok_core.boundary.sensitivity import Labelled
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

# Module attributes, never bound names (found 2026-09-26): tests and Tasks 6, 12 and 26A patch
# `selector_drift.browser.registry.url_for`, and `from ...registry import url_for` here would
# bind the original at import time and make every one of those patches vacuous.
from selector_drift.browser import allowlist, ratelimit, registry
from selector_drift.browser.allowlist import OffScopeNavigation, RequestLedger
from selector_drift.heallog.capture import VIEWPORT
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.record import AuthorisedTarget

_NO_SANDBOX_ENV = "SD_CHROMIUM_NO_SANDBOX"


def _agent_version() -> str:
    try:
        return version("selector-drift")
    except PackageNotFoundError:
        return "0"


def _user_agent(target: AuthorisedTarget) -> str:
    """Identify the agent and give a contact URL (CLAUDE.md, never-rule 3). Deliberately not a
    stock browser string: this driver does not pretend to be a person."""
    agent = f"selector-drift/{_agent_version()}"
    return f"Mozilla/5.0 (compatible; {agent}; +{target.user_agent_contact})"


def _launch_args() -> list[str]:
    # WebRTC UDP bypasses both the egress proxy and the route handler (H2): allow only
    # proxied UDP, which with a dead proxy is none.
    args = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    # Only inside the worker container (docker-compose sets it), where Docker's default seccomp
    # profile denies what Chromium's own sandbox needs; see the compose `worker` comment.
    if os.environ.get(_NO_SANDBOX_ENV) == "1":
        args.append("--no-sandbox")
    return args


async def _navigate(page: Page, url: str, ledger: RequestLedger) -> None:
    """I-2. An off-scope redirect hop fails at the dead proxy with a generic Playwright net
    error; report it as the scope violation it is, so an orchestrator refuses, not retries."""
    try:
        await page.goto(url)
    except PlaywrightError as exc:
        error = ledger.off_scope_error()
        if error is not None:
            raise error from exc
        raise
    ledger.raise_if_off_scope()


def _resolve_step_path(base: str, step_path: object, target: AuthorisedTarget) -> str:
    """A step names a path on the target, never a URL. Anything that could leave the origin --
    an absolute URL, a scheme-relative `//host`, a backslash (browsers read `/\\host` as
    `//host`), a control character -- is refused here, before any request exists."""
    if (
        not isinstance(step_path, str)
        or not step_path.startswith("/")
        or step_path.startswith("//")
        or "\\" in step_path
        or any(ord(c) < 0x21 for c in step_path)
    ):
        raise OffScopeNavigation(f"step path {step_path!r} is not a path on the target")
    resolved = urljoin(base, step_path)
    if not allowlist.is_allowed(resolved, target):
        raise OffScopeNavigation(f"step path {step_path!r} resolves off the authorised origins")
    return resolved


class PageHandle:
    """The page, as the rest of the product may see it. No method takes a URL."""

    def __init__(
        self, page: Page, target: AuthorisedTarget, base: str, ledger: RequestLedger
    ) -> None:
        self._page = page
        self._target = target
        self._base = base
        self._ledger = ledger

    @property
    def url(self) -> str:
        return self._page.url

    async def content(self) -> Labelled[str]:
        """The raw DOM, labelled with the target's sensitivity where it is read (S-M6). It is
        unscrubbed: nothing may persist it -- evidence goes through heallog.evidence.store."""
        self._ledger.raise_if_off_scope()
        return Labelled(await self._page.content(), self._target.sensitivity)

    async def click(self, selector: str) -> None:
        """A click may navigate, so it takes a rate-limit token first; if it tried to leave the
        authorised origins, this raises `OffScopeNavigation`."""
        self._ledger.raise_if_off_scope()
        await asyncio.to_thread(ratelimit.acquire, self._target)
        try:
            await self._page.click(selector)
            await self._page.wait_for_load_state("load")
        except PlaywrightError as exc:
            error = self._ledger.off_scope_error()
            if error is not None:
                raise error from exc
            raise
        self._ledger.raise_if_off_scope()

    async def goto_step(self, step: Mapping[str, object]) -> None:
        """`{"step": "goto", "path": "/results"}` -- a path, resolved against the registry URL."""
        self._ledger.raise_if_off_scope()
        if step.get("step") != "goto":
            raise ValueError(f"not a goto step: {step!r}")
        resolved = _resolve_step_path(self._base, step.get("path"), self._target)
        await asyncio.to_thread(ratelimit.acquire, self._target)
        await _navigate(self._page, resolved, self._ledger)


def _playwright_page(handle: PageHandle) -> Page:
    """browser/-internal: the raw Playwright page behind a handle, for page_source.py's
    read-only snapshot. Its goto()/click() would skip ratelimit.acquire() and
    raise_if_off_scope(), so nothing outside browser/ may call this --
    tests/test_playwright_page_boundary.py enforces that (fix round 1, S-I1)."""
    handle._ledger.raise_if_off_scope()
    return handle._page


@dataclass(frozen=True)
class Session:
    page: PageHandle
    _ledger: RequestLedger

    @property
    def requested_urls(self) -> list[str]:
        return list(self._ledger.requested_urls)

    @property
    def blocked_urls(self) -> list[str]:
        return list(self._ledger.blocked_urls)


@asynccontextmanager
async def open(target: AuthorisedTarget) -> AsyncIterator[Session]:
    """One run: one browser, one context, the target's registry URL loaded, torn down on exit.

    No credentials parameter yet -- `Credentials` arrives with Task 31."""
    if not isinstance(target, AuthorisedTarget):
        raise TypeError("open() takes an AuthorisedTarget minted by scope.gate.authorise()")
    # Authorised yesterday is not authorised today: a long-lived orchestrator may hold a
    # target past its expiry, so check again here, before anything is launched (fails closed).
    if target.expires_on < datetime.now(UTC).date():
        raise ScopeRefusal(f"authorisation for {target.target!r} expired on {target.expires_on}")
    base = registry.url_for(target.target)
    if not allowlist.is_allowed(base, target):
        raise OffScopeNavigation(f"registry URL for {target.target!r} is outside its origins")

    ledger = RequestLedger()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=_launch_args(), proxy=allowlist.proxy_for(target)
        )
        try:
            # Fixed viewport on every context (Task 19): recorded boxes are only comparable
            # across runs at one size, and CSS px == screenshot px only at a DPR of 1.
            context = await browser.new_context(
                viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
                device_scale_factor=1,
                user_agent=_user_agent(target),
                service_workers="block",
                accept_downloads=False,
            )
            try:
                await allowlist.install(context, target, ledger)
                page = await context.new_page()
                handle = PageHandle(page, target, base, ledger)
                await asyncio.to_thread(ratelimit.acquire, target)
                await _navigate(page, base, ledger)
                yield Session(page=handle, _ledger=ledger)
            finally:
                await context.close()
        finally:
            await browser.close()
