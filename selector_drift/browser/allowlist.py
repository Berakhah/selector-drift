"""R8/H2: the browser never leaves the origins the scope record authorised.

The scope gate's guarantee is *"no path to a page without a scope record"*. That is true of the
first navigation and of nothing after it unless every later request is held to the same
origins. Two layers, because neither is sufficient alone:

1. **A Playwright route handler** (`install`). Sees every request the page *initiates* -- the
   first hop of a navigation, every subresource, every fetch/XHR -- and aborts any whose origin
   is not in `target.origins`. It is the layer that records what was requested and what was
   refused.

2. **A dead egress proxy** (`proxy_for`). Playwright's route handler is called *only for the
   first URL of a redirect chain* (documented, and reproduced for Task 4: a 302 from an in-scope
   page to an off-scope URL reached the network with a route handler alone). So Chromium is
   launched with every request sent to a proxy at an `.invalid` host that can never resolve
   (RFC 6761), bypassed only for the exact authorised origins. A redirect hop, a service
   worker's fetch or a WebSocket to anywhere else fails inside Chromium's network stack with
   `ERR_PROXY_CONNECTION_FAILED` -- before any byte leaves for the off-scope host.

A `context.on("request")` listener sees the redirect hops the route handler cannot, so they
are still recorded (as requested if in scope, as blocked if not) and an off-scope *main-frame*
navigation is still reported to the driver, which raises `OffScopeNavigation` on the next
operation. An off-scope iframe is aborted and recorded in `blocked_urls` like any subresource,
but does not mark the run off-scope: the page the run is on is still the authorised one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, CDPSession, Page, ProxySettings, Request, Route
from playwright.async_api import Error as PlaywrightError

from selector_drift.scope.origin import origin_of_url, parse_origin
from selector_drift.scope.record import AuthorisedTarget

# RFC 6761: `.invalid` never resolves. Nothing can be listening at this address, so a request
# routed to it cannot succeed, and Chromium does not fall back to DIRECT for an explicit proxy.
_DEAD_PROXY: Final = "http://egress-denied.invalid:1"


class OffScopeNavigation(RuntimeError):
    """A navigation tried to leave the authorised origins. The run must stop, not continue on
    whatever page it did or did not reach."""


class WriteBlocked(OffScopeNavigation):
    """A request that changes state (anything but GET, HEAD or OPTIONS) outside the window the
    driver opens for the spec's navigate steps. CLAUDE.md never-rule 7: no write-back into a
    client portal in v1. A scope violation like the rest of this module's, so the orchestrator
    refuses the run instead of retrying it."""


# Requests that do not change state. Everything else is a write.
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


def _is_write(method: str) -> bool:
    return method.upper() not in _SAFE_METHODS


def _describe(method: str, url: str) -> str:
    """`POST https://host/path` -- never the query or fragment, which can carry an identifier
    and end up in a log line, an error message and a heal event."""
    parts = urlsplit(url)
    return f"{method.upper()} {parts.scheme}://{parts.netloc}{parts.path}"


def origin_of(url: str) -> str | None:
    """`scheme://host:port`, lower-cased, with the default port made explicit, so
    `https://A.example` and `https://a.example:443/x` compare equal. None for anything that is
    not an absolute http(s) URL with a strict host (I-1: no `*`, `,`, `;`, whitespace, `<...>`
    or userinfo) -- which is never in scope. One rule, shared with the scope record."""
    return origin_of_url(url)


def allowed_origins(target: AuthorisedTarget) -> frozenset[str]:
    """Canonical origins. `authorise()` has already validated every one of them."""
    return frozenset(parse_origin(o) for o in target.origins)


def is_allowed(url: str, target: AuthorisedTarget) -> bool:
    origin = origin_of(url)
    return origin is not None and origin in allowed_origins(target)


def proxy_for(target: AuthorisedTarget) -> ProxySettings:
    """Everything goes to the dead proxy except the authorised origins.

    `<-loopback>` must come FIRST: it removes Chromium's implicit loopback bypass (otherwise
    every `localhost`/`127.0.0.1` port would be reachable), and Chromium evaluates bypass rules
    in order -- measured for Task 4, with it last the authorised origin itself was proxied.
    Rules are scheme-qualified (`http://127.0.0.1:8000`) so a different scheme or port on an
    authorised host is still refused."""
    origins = sorted(allowed_origins(target))
    if not origins:
        raise OffScopeNavigation(f"target {target.target!r} has no usable origin")
    return {"server": _DEAD_PROXY, "bypass": ",".join(["<-loopback>", *origins])}


@dataclass
class RequestLedger:
    """What the page asked for during one run. `requested_urls` were allowed through;
    `blocked_urls` were refused. `off_scope_navigation` holds the first refused main-frame
    navigation, for the driver to raise on."""

    requested_urls: list[str] = field(default_factory=list)
    blocked_urls: list[str] = field(default_factory=list)
    off_scope_navigation: str | None = None
    # Never-rule 7. `writes_allowed` is true only inside PageHandle.click_role(signs_in=True),
    # which navigate.run_steps uses for the click after a credential fill (logging in is a
    # POST) and which lets ONE write through: a main-frame POST. Any other write is refused
    # and recorded here as `METHOD scheme://host/path`; a refused write that is a document
    # navigation (a form submit) also stops the run.
    writes_allowed: bool = False
    blocked_writes: list[str] = field(default_factory=list)
    write_attempt: str | None = None

    def refuse(self, url: str, *, navigation: bool) -> None:
        self.blocked_urls.append(url)
        if navigation and self.off_scope_navigation is None:
            self.off_scope_navigation = url

    def refuse_write(self, method: str, url: str, *, stops_run: bool = True) -> None:
        """Record a refused write. A beacon or a POST-based XHR is aborted and listed but, being
        no navigation, does not fail the run (`stops_run=False`): the portal never saw it."""
        described = _describe(method, url)
        self.blocked_writes.append(described)
        if stops_run and self.write_attempt is None:
            self.write_attempt = described

    def off_scope_error(self) -> OffScopeNavigation | None:
        if self.write_attempt is not None:
            return WriteBlocked(
                f"{self.write_attempt} was refused: the product does not write into a portal "
                "(CLAUDE.md never-rule 7)"
            )
        if self.off_scope_navigation is None:
            return None
        return OffScopeNavigation(
            f"navigation to {self.off_scope_navigation!r} is outside the authorised origins"
        )

    def raise_if_off_scope(self) -> None:
        error = self.off_scope_error()
        if error is not None:
            raise error


def is_main_frame_navigation(request: Request) -> bool:
    """True only for a navigation of the page's top-level frame. An aborted iframe or
    subresource is refused and recorded, but it is not the *run* leaving scope -- the page the
    run is on is still the authorised one. `request.frame` raises for a service-worker
    request, which has no frame; that is never a main-frame navigation."""
    if not request.is_navigation_request():
        return False
    try:
        frame = request.frame
    except PlaywrightError:  # "Service Worker requests do not have an associated frame"
        return False
    return frame.parent_frame is None


async def install(
    context: BrowserContext, target: AuthorisedTarget, ledger: RequestLedger | None = None
) -> None:
    """Route every request through the allowlist. Must be installed before the first page is
    opened, and used together with `proxy_for` at launch (see the module docstring). Pass a
    `ledger` to have what was requested and refused recorded on it."""
    if ledger is None:
        ledger = RequestLedger()
    allowed = allowed_origins(target)

    async def _route(route: Route) -> None:
        request = route.request
        if origin_of(request.url) in allowed:
            if _is_write(request.method):
                await _route_write(route, ledger)
                return
            ledger.requested_urls.append(request.url)
            await route.continue_()
        else:
            ledger.refuse(request.url, navigation=is_main_frame_navigation(request))
            await route.abort("blockedbyclient")

    async def _route_write(route: Route, ledger: RequestLedger) -> None:
        request = route.request
        navigation = is_main_frame_navigation(request)
        # The window lets exactly one write through, and only the sign-in's own: a POST that is
        # a main-frame navigation (the form submit). A script's fetch/XHR/beacon, a PUT or a
        # DELETE inside the window is refused and does not spend it, so a heartbeat fired during
        # the click cannot take the place of the sign-in.
        if not (ledger.writes_allowed and request.method.upper() == "POST" and navigation):
            ledger.refuse_write(request.method, request.url, stops_run=navigation)
            await route.abort("blockedbyclient")
            return
        # Playwright routes only the first hop of a redirect chain, so a 307/308 replay of that
        # POST is failed by `guard_redirects` (a CDP Fetch session), not here.
        ledger.writes_allowed = False
        ledger.requested_urls.append(request.url)
        await route.continue_()

    def _on_request(request: Request) -> None:
        # Only redirect hops: the first hop of every chain went through `_route` above.
        if request.redirected_from is None:
            return
        if origin_of(request.url) in allowed:
            if _is_write(request.method) and not ledger.writes_allowed:
                # Backstop: `guard_redirects` fails a replayed write before it is sent. If that
                # guard is not attached, the hop can only be recorded here, after the fact.
                ledger.refuse_write(request.method, request.url)
            ledger.requested_urls.append(request.url)
        else:
            ledger.refuse(request.url, navigation=is_main_frame_navigation(request))

    context.on("request", _on_request)
    await context.route("**/*", _route)


async def guard_redirects(
    context: BrowserContext, ledger: RequestLedger
) -> Callable[[Page], Awaitable[None]]:
    """Fail a redirect hop that would replay a write, before Chromium sends it.

    A 307/308 keeps the method and body, so the portal's answer to the sign-in POST can make
    Chromium send that POST again at the `Location`. Playwright's route handler never sees
    the hop (see the module docstring) and the `request` listener only hears of it once it is
    on the wire. A raw CDP `Fetch` session does see it, flagged by `redirectedRequestId`, and
    can refuse it: never-rule 7 holds for the replay as it does for the first hop.

    Returns `guard(page)`, which resolves once Fetch is enabled on that page. The driver awaits
    it before the page's first navigation, so no request precedes the guard; a popup is guarded
    by the context's 'page' event, once. Residual: the session is on the page target, so a
    cross-origin iframe's own redirect chain is not seen -- a write there would need the sign-in
    window, which only a main-frame click opens.

    A request the guard cannot judge is failed, not continued and not left paused.
    """
    guarded: dict[Page, asyncio.Task[None]] = {}
    pending: set[asyncio.Task[None]] = set()  # a task nothing references can be collected early

    def _spawn(coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = asyncio.ensure_future(coro)
        pending.add(task)
        task.add_done_callback(pending.discard)
        return task

    async def _decide(session: CDPSession, paused: dict[str, Any]) -> None:
        request_id = paused.get("requestId")
        try:
            request = paused["request"]
            method, url = str(request["method"]), str(request["url"])
            if paused.get("redirectedRequestId") is not None and _is_write(method):
                ledger.refuse_write(method, url)
                await session.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                )
            else:
                await session.send("Fetch.continueRequest", {"requestId": request_id})
        except PlaywrightError:
            pass  # the page or its target closed while the request was paused
        except Exception:
            # Fail closed: a request we cannot judge is neither sent nor left paused.
            with contextlib.suppress(PlaywrightError):
                await session.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                )

    async def _attach(page: Page) -> None:
        session = await context.new_cdp_session(page)
        session.on("Fetch.requestPaused", lambda p: _spawn(_decide(session, p)))
        await session.send(
            "Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
        )

    def guard(page: Page) -> Awaitable[None]:
        if page not in guarded:
            guarded[page] = asyncio.ensure_future(_attach(page))
        return guarded[page]

    context.on("page", lambda page: _spawn(_as_coro(guard(page))))
    for page in context.pages:
        await guard(page)
    return guard


async def _as_coro(awaitable: Awaitable[None]) -> None:
    await awaitable
