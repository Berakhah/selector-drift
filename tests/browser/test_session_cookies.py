"""Task 26A carried item (ledger 632): the first time the driver hands SessionInjection cookies to
Playwright, prove ON THE WIRE in a real Chromium that

(a) a cookie whose domain has no leading dot is HOST-ONLY: Chromium does not send it to a
    subdomain of the authorised host -- while a control cookie WITH a leading dot does reach that
    subdomain, so the check is not vacuous. The hosts are resolver-mapped to loopback
    (`--host-resolver-rules`): `localhost` cannot serve, because Chromium sends no domain cookie
    to `sub.localhost` at all and the control would fail.
(b) a `__Host-` cookie survives the provider's host-only rewrite and is sent by driver.open(). The
    plan suggested scoping it by `url=`; Chromium refuses that form for a Secure cookie on an http
    origin ("Invalid cookie fields", found here), so the host-only `domain` is kept. `localhost`
    is a secure context, so a Secure cookie is sent over plain http to it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import async_playwright

from selector_drift.browser import driver
from selector_drift.credentials.provider import Credentials, select_cookies
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import AuthorisedTarget, Basis, ScopeRecord

TARGET = "driver-fixture"  # the browser tests' own target; its limiter rows are cleaned


class _Echo:
    def __init__(self) -> None:
        self.cookie_headers: list[str] = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = server.server_address[1]
        self.server = server

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        echo = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                echo.cookie_headers.append(self.headers.get("Cookie", ""))
                body = b"<!doctype html><html><body><p>ok</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


@pytest.fixture
def echo() -> Iterator[_Echo]:
    e = _Echo()
    thread = threading.Thread(target=e.server.serve_forever, daemon=True)
    thread.start()
    try:
        yield e
    finally:
        e.server.shutdown()
        e.server.server_close()


def _target(origin: str) -> AuthorisedTarget:
    today = datetime.now(UTC).date()
    record = ScopeRecord(
        target=TARGET,
        basis=Basis.SELF_OWNED,
        authorising_party="selector-drift test suite",
        authorised_on=today - timedelta(days=1),
        expires_on=today + timedelta(days=365),
        evidence_uri=None,
        evidence_sha256=None,
        origins=[origin],
        rate_limit={"requests": 10, "per_seconds": 1},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
    )
    return authorise(record, today=today)


def _storage_state_cookies(expires: float) -> list[dict[str, object]]:
    """What a client's exported Playwright storage state holds: a parent-domain cookie (the case
    H2 rewrites to host-only) and a `__Host-` cookie."""
    return [
        {"name": "sid", "value": "test-only-session", "domain": ".localhost", "path": "/",
         "expires": expires, "httpOnly": True, "secure": False, "sameSite": "Lax"},
        {"name": "__Host-csrf", "value": "test-only-csrf", "domain": "localhost", "path": "/",
         "expires": expires, "httpOnly": True, "secure": True, "sameSite": "Lax"},
        {"name": "__Secure-ctx", "value": "test-only-secure", "domain": ".localhost", "path": "/",
         "expires": expires, "httpOnly": True, "secure": True, "sameSite": "Lax"},
    ]  # fmt: skip


def _selected(origin: str) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    picked = select_cookies(
        _storage_state_cookies((now + timedelta(days=1)).timestamp()), [origin], now
    )
    assert picked.off_origin == 0 and len(picked.live) == 3
    return picked.live


MAPPED = "app.sd-test.example"  # resolver-mapped to 127.0.0.1 below; never looked up


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_a_cookie_the_driver_hands_over_is_host_only_in_chromium(echo: _Echo) -> None:
    origin = f"http://{MAPPED}:{echo.port}"
    now = datetime.now(UTC)
    expires = (now + timedelta(days=1)).timestamp()
    stored = [{"name": "sid", "value": "test-only-session", "domain": f".{MAPPED}",
               "path": "/", "expires": expires, "secure": False, "sameSite": "Lax"}]  # fmt: skip
    picked = select_cookies(stored, [origin], now)
    handed = driver._playwright_cookies(picked.live)
    assert [c.get("domain") for c in handed] == [MAPPED]  # the parent-domain cookie, narrowed
    control = {"name": "control", "value": "domain-cookie", "domain": f".{MAPPED}", "path": "/",
               "expires": expires, "secure": False, "sameSite": "Lax"}  # fmt: skip
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--host-resolver-rules=MAP *.sd-test.example 127.0.0.1"]
        )
        try:
            context = await browser.new_context()
            await context.add_cookies([*handed, control])  # type: ignore[list-item]
            page = await context.new_page()
            await page.goto(f"{origin}/")
            await page.goto(f"http://x.{MAPPED}:{echo.port}/")
        finally:
            await browser.close()
    on_host, on_sub = echo.cookie_headers
    assert "sid=test-only-session" in on_host and "control=domain-cookie" in on_host
    assert "control=domain-cookie" in on_sub  # a domain cookie DOES reach the subdomain ...
    assert "sid=" not in on_sub  # ... and the driver's host-only cookie does not


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_open_sends_a_host_prefixed_cookie_to_the_authorised_origin(
    echo: _Echo, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = f"http://localhost:{echo.port}"
    target = _target(origin)
    monkeypatch.setattr("selector_drift.browser.registry.url_for", lambda _t: f"{origin}/")
    async with driver.open(target, Credentials(cookies=_selected(origin))) as session:
        assert "ok" in (await session.page.content()).value
    sent = echo.cookie_headers[0]
    assert "sid=test-only-session" in sent and "__Host-csrf=test-only-csrf" in sent


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_open_sends_a_secure_prefixed_cookie_after_the_domain_rewrite(
    echo: _Echo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1, M2. A `__Secure-` cookie is exported with a parent domain (`.localhost`),
    so the host-only rewrite touches it -- and Chromium must still accept the rewritten cookie
    (the prefix demands only `Secure`; unlike `__Host-` it may carry a Domain) and send it."""
    origin = f"http://localhost:{echo.port}"
    selected = _selected(origin)
    handed = driver._playwright_cookies(selected)
    secure = next(c for c in handed if c["name"] == "__Secure-ctx")
    assert secure.get("domain") == "localhost" and secure.get("secure") is True
    monkeypatch.setattr("selector_drift.browser.registry.url_for", lambda _t: f"{origin}/")
    async with driver.open(_target(origin), Credentials(cookies=selected)) as session:
        assert "ok" in (await session.page.content()).value
    assert "__Secure-ctx=test-only-secure" in echo.cookie_headers[0]


@pytest.mark.unit
def test_a_parent_domain_cookie_never_reaches_playwright() -> None:
    """Defence in depth: select_cookies already rewrites, but the driver refuses one it missed."""
    with pytest.raises(ValueError, match="parent domain"):
        driver._playwright_cookies([{"name": "sid", "value": "x", "domain": ".localhost"}])
