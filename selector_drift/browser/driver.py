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
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from bok_core.boundary.sensitivity import Labelled
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

# Module attributes, never bound names (found 2026-09-26): tests and Tasks 6, 12 and 26A patch
# `selector_drift.browser.registry.url_for`, and `from ...registry import url_for` here would
# bind the original at import time and make every one of those patches vacuous.
from selector_drift.browser import allowlist, ratelimit, registry
from selector_drift.browser.allowlist import OffScopeNavigation, RequestLedger
from selector_drift.credentials.provider import Credentials
from selector_drift.heallog.capture import VIEWPORT
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.record import AuthorisedTarget

if TYPE_CHECKING:
    # Playwright exports this TypedDict only from its private module; types only.
    from playwright._impl._api_structures import SetCookieParam as _Cookie

_NO_SANDBOX_ENV = "SD_CHROMIUM_NO_SANDBOX"
_FIELD = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # a form field name, never a selector
_ROLE = re.compile(r"^[a-z]{2,20}$")  # an ARIA role name (spec.schema.NavStep)


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


# What Chromium needs from its parent environment to start and render, and nothing else. An
# allowlist: Playwright gives the browser the parent's whole environment unless `env=` is passed,
# and the worker's holds the database password, the redaction key and any credential KEK. With
# --no-sandbox a renderer compromise could read them from /proc/<pid>/environ.
_BROWSER_ENV_ALLOW = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LD_LIBRARY_PATH",
        "FONTCONFIG_PATH",
        "XDG_RUNTIME_DIR",
        "DISPLAY",
        # Windows: Chromium will not start without these.
        "SYSTEMROOT",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
    }
)


def _browser_env() -> dict[str, str | float | bool]:
    # The wide value type is Playwright's own signature for `env`; dict is invariant.
    return {k: v for k, v in os.environ.items() if k in _BROWSER_ENV_ALLOW}


def _launch_args() -> list[str]:
    # WebRTC UDP bypasses both the egress proxy and the route handler (H2): allow only
    # proxied UDP, which with a dead proxy is none.
    args = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    # Only inside the worker container (docker-compose sets it), where Docker's default seccomp
    # profile denies what Chromium's own sandbox needs; see the compose `worker` comment.
    if os.environ.get(_NO_SANDBOX_ENV) == "1":
        args.append("--no-sandbox")
    return args


class PortalUnavailable(RuntimeError):
    """A navigation the portal answered with a server error (5xx). The run fails and is retried;
    nothing is resolved or extracted from an error page (Task 26A)."""


async def _navigate(page: Page, url: str, ledger: RequestLedger) -> None:
    """I-2. An off-scope redirect hop fails at the dead proxy with a generic Playwright net
    error; report it as the scope violation it is, so an orchestrator refuses, not retries."""
    try:
        response = await page.goto(url)
    except PlaywrightError as exc:
        error = ledger.off_scope_error()
        if error is not None:
            raise error from exc
        raise
    ledger.raise_if_off_scope()
    if response is not None and response.status >= 500:
        # The status only: the URL may carry a client's identifiers and the body is page text.
        raise PortalUnavailable(f"the portal answered HTTP {response.status}")


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

    async def click_role(self, role: str, name: str) -> None:
        """A spec's `click` step: the element with this ARIA role and exact accessible name.
        It owns the rate-limit token, like `click()` (ruling, Important 5): a click may
        navigate, and navigate.run_steps takes no token of its own."""
        if not _ROLE.fullmatch(role):
            raise ValueError(f"{role!r} is not an ARIA role name")
        self._ledger.raise_if_off_scope()
        await asyncio.to_thread(ratelimit.acquire, self._target)
        try:
            await self._page.get_by_role(role, name=name, exact=True).click()  # type: ignore[arg-type]
            await self._page.wait_for_load_state("load")
        except PlaywrightError as exc:
            error = self._ledger.off_scope_error()
            if error is not None:
                raise error from exc
            raise
        self._ledger.raise_if_off_scope()

    async def fill_field(self, field: str, value: str) -> None:
        """A spec's `fill` step: the form control named `field`. Never navigates, so it takes
        no rate-limit token. The value may be a credential: it is never logged or raised."""
        if not _FIELD.fullmatch(field):
            raise ValueError("fill takes a plain form field name, never a selector")
        self._ledger.raise_if_off_scope()
        await self._page.locator(f"[name='{field}']").fill(value)

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


def _playwright_cookies(cookies: Sequence[Mapping[str, Any]]) -> list[_Cookie]:
    """The provider has already narrowed every cookie to a host-only clone for an authorised
    host (credentials.provider.select_cookies, H2): `domain` with no leading dot, which Chromium
    stores host-only. A `__Host-` cookie passes through as it is -- host-only, Secure, Path=/ is
    exactly what its prefix demands, and Chromium refuses the `url=` form for it on an http
    origin. Both properties are pinned on the wire by tests/browser/test_session_cookies.py
    (Task 26A carried item). A parent-domain cookie that somehow reached here is refused."""
    out: list[_Cookie] = []
    for raw in cookies:
        if str(raw.get("domain", "")).startswith("."):
            raise ValueError("a cookie reached the driver scoped to a parent domain (H2)")
        out.append(dict(raw))  # type: ignore[arg-type]
    return out


@asynccontextmanager
async def open(
    target: AuthorisedTarget, credentials: Credentials | None = None
) -> AsyncIterator[Session]:
    """One run: one browser, one context, the target's registry URL loaded, torn down on exit.

    `credentials.cookies` (session injection, Task 31) are added to the context before the
    first request; a username and password are used only by navigate.run_steps' fill steps."""
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
            headless=True,
            args=_launch_args(),
            proxy=allowlist.proxy_for(target),
            env=_browser_env(),
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
                if credentials is not None and credentials.cookies:
                    await context.add_cookies(_playwright_cookies(credentials.cookies))
                page = await context.new_page()
                handle = PageHandle(page, target, base, ledger)
                await asyncio.to_thread(ratelimit.acquire, target)
                await _navigate(page, base, ledger)
                yield Session(page=handle, _ledger=ledger)
            finally:
                await context.close()
        finally:
            await browser.close()
