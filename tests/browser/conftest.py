"""Test-local fixtures for the browser driver. NOT the product test bench (Task 26A).

A stdlib `http.server` on 127.0.0.1:<random port> stands in for an authorised portal:

- ``/``         a trivial page with ``<a id="leaves-the-portal" href="/evil">``
- ``/evil``     a 302 to ``http://localhost:<port>/`` -- the same socket, but a *different
                origin string* from the authorised ``http://127.0.0.1:<port>``, so it is off
                scope. Nothing needs to answer there: the driver must never send the request.
- ``/results``  a page embedding an off-origin ``<img src="http://localhost:<port>/x.png">``
- ``/framed``   a page embedding an off-origin ``<iframe src="http://localhost:<port>/widget">``
                -- a sub-frame navigation, aborted and recorded, but not the run leaving scope

The target is named ``driver-fixture``, deliberately NOT one of the registry's real targets:
``registry.url_for`` is monkeypatched for it, and the autouse limiter cleanup below deletes only
this name's ``audit_log`` rows, so it can never touch a real target's rate-limit history.

The server records every request line it receives (with its Host header) so a test can prove
an off-scope request never reached the wire, not merely that the driver says it blocked it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from selector_drift.scope.gate import authorise
from selector_drift.scope.record import AuthorisedTarget, Basis, ScopeRecord

TARGET = "driver-fixture"


@dataclass
class Bench:
    port: int
    base_url: str
    origin: str
    seen: list[tuple[str, str]] = field(default_factory=list)  # (Host header, path)

    @property
    def off_origin(self) -> str:
        return f"http://localhost:{self.port}"


def _handler(bench: Bench) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str = "text/html") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            bench.seen.append((self.headers.get("Host", ""), self.path))
            if self.path == "/":
                self._send(
                    200,
                    b"<!doctype html><html><head><title>bench</title></head><body>"
                    b'<a id="leaves-the-portal" href="/evil">leave</a></body></html>',
                )
            elif self.path == "/evil":
                self.send_response(302)
                self.send_header("Location", f"{bench.off_origin}/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path == "/results":
                self._send(
                    200,
                    b"<!doctype html><html><head><title>results</title></head><body>"
                    b"<table><tr><td>AB12345678</td></tr></table>"
                    + f'<img src="{bench.off_origin}/x.png" alt="">'.encode()
                    + b"</body></html>",
                )
            elif self.path == "/framed":
                self._send(
                    200,
                    b"<!doctype html><html><head><title>framed</title></head><body>"
                    b"<p>host page</p>"
                    + f'<iframe src="{bench.off_origin}/widget"></iframe>'.encode()
                    + b"</body></html>",
                )
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


@pytest.fixture(scope="session")
def bench() -> Iterator[Bench]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    b = Bench(port=port, base_url=f"http://127.0.0.1:{port}/", origin=f"http://127.0.0.1:{port}")
    server.RequestHandlerClass = _handler(b)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield b
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def authorised_testbench(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> AuthorisedTarget:
    """Minted through `authorise()` like every other target -- never through the token."""
    today = datetime.now(UTC).date()
    record = ScopeRecord(
        target=TARGET,
        basis=Basis.SELF_OWNED,
        authorising_party="selector-drift test suite",
        authorised_on=today - timedelta(days=1),
        expires_on=today + timedelta(days=365),
        evidence_uri=None,
        evidence_sha256=None,
        origins=[bench.origin],
        rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
    )
    # Covers "driver-fixture", which is deliberately absent from registry._URLS.
    monkeypatch.setattr("selector_drift.browser.registry.url_for", lambda _target: bench.base_url)
    return authorise(record, today=today)


@pytest.fixture
def evil_redirect(bench: Bench) -> str:
    """The URL of an in-scope page (`/evil`) that 302s to an off-origin URL. Clicking the
    bench's `#leaves-the-portal` link lands here; the redirect hop is the part a Playwright
    route handler never sees, which is why the driver needs more than a route handler."""
    return f"{bench.base_url}evil"


@pytest.fixture(autouse=True)
def _fresh_rate_limit_bucket(request: pytest.FixtureRequest) -> None:
    """Delete this target's limiter rows before each browser test, so tests do not wait on
    one another's acquisitions. Unit tests never touch Postgres, so they skip this."""
    if request.node.get_closest_marker("unit") is not None:
        return
    from sqlalchemy import delete

    from selector_drift.config import get_settings
    from selector_drift.db.models import AuditLog
    from selector_drift.db.session import Session
    from tests.conftest import require_usable_test_database

    # The URL the engine will actually use: with SD_DATABASE_URL unset that is the development
    # database, which this fixture's DELETE must never reach.
    require_usable_test_database(get_settings().database_url)
    with Session() as s, s.begin():
        s.execute(
            delete(AuditLog).where(
                AuditLog.action == "ratelimit.acquire", AuditLog.subject == TARGET
            )
        )
