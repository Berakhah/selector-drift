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

from dataclasses import dataclass, field
from typing import Final

from playwright.async_api import BrowserContext, ProxySettings, Request, Route
from playwright.async_api import Error as PlaywrightError

from selector_drift.scope.origin import origin_of_url, parse_origin
from selector_drift.scope.record import AuthorisedTarget

# RFC 6761: `.invalid` never resolves. Nothing can be listening at this address, so a request
# routed to it cannot succeed, and Chromium does not fall back to DIRECT for an explicit proxy.
_DEAD_PROXY: Final = "http://egress-denied.invalid:1"


class OffScopeNavigation(RuntimeError):
    """A navigation tried to leave the authorised origins. The run must stop, not continue on
    whatever page it did or did not reach."""


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

    def refuse(self, url: str, *, navigation: bool) -> None:
        self.blocked_urls.append(url)
        if navigation and self.off_scope_navigation is None:
            self.off_scope_navigation = url

    def off_scope_error(self) -> OffScopeNavigation | None:
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
            ledger.requested_urls.append(request.url)
            await route.continue_()
        else:
            ledger.refuse(request.url, navigation=is_main_frame_navigation(request))
            await route.abort("blockedbyclient")

    def _on_request(request: Request) -> None:
        # Only redirect hops: the first hop of every chain went through `_route` above.
        if request.redirected_from is None:
            return
        if origin_of(request.url) in allowed:
            ledger.requested_urls.append(request.url)
        else:
            ledger.refuse(request.url, navigation=is_main_frame_navigation(request))

    context.on("request", _on_request)
    await context.route("**/*", _route)
