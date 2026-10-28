"""CLAUDE.md never-rule 7: no write-back into a client portal in v1.

Until now that rule held only because the product has no write API. The browser itself would have
let a POST, PUT, PATCH or DELETE through to any authorised origin -- a spec or a heal that landed
on "Delete" or "Approve", a portal link that changes state on GET aside, would have reached the
client's system of record, and nothing in between looked at the method.

Now the route handler refuses every request that is not GET, HEAD or OPTIONS, and records it so
the run stops. The one exception is a window the driver opens around the spec's navigate steps:
logging in is a POST, and the steps are reviewed spec, not a heal's guess.

These are unit tests over fake Playwright objects: the policy lives in `allowlist.install`. The
other half -- the real portal's login still works, and a POST after it is refused -- is
tests/orchestrate/test_write_block_bench.py.

Callers: none (pytest collects tests/browser/test_*.py). Instruction: "test it with the existing
bench".
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from selector_drift.browser import allowlist
from selector_drift.browser.allowlist import OffScopeNavigation, RequestLedger, WriteBlocked
from selector_drift.browser.driver import PageHandle
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import Basis, ScopeRecord

ORIGIN = "https://portal.example.invalid"
PAGE = f"{ORIGIN}/claims"


def _target() -> Any:
    today = dt.date(2026, 9, 7)
    return authorise(
        ScopeRecord(
            target="a-client",
            basis=Basis.SELF_OWNED,
            authorising_party="me",
            authorised_on=dt.date(2026, 9, 1),
            expires_on=dt.date(2027, 9, 1),
            evidence_uri=None,
            evidence_sha256=None,
            origins=[ORIGIN],
            rate_limit={"requests": 1, "per_seconds": 30},
            user_agent_contact="https://example.invalid/contact",
            first_heal_policy="required",
            legal_review="self_drafted",
        ),
        today=today,
    )


class _Request:
    def __init__(
        self, url: str, method: str = "GET", *, redirected: bool = False, navigation: bool = True
    ) -> None:
        self.url, self.method = url, method
        self.redirected_from = object() if redirected else None
        self.frame = SimpleNamespace(parent_frame=None)
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class _Route:
    def __init__(self, request: _Request) -> None:
        self.request = request
        self.continued = False
        self.aborted: str | None = None

    async def continue_(self) -> None:
        self.continued = True

    async def abort(self, reason: str) -> None:
        self.aborted = reason


class _Context:
    """Just enough of a BrowserContext for allowlist.install to hang its two handlers on."""

    def __init__(self) -> None:
        self.route_handler: Any = None
        self.request_handler: Any = None

    def on(self, event: str, handler: Any) -> None:
        assert event == "request"
        self.request_handler = handler

    async def route(self, _pattern: str, handler: Any) -> None:
        self.route_handler = handler


async def _installed(ledger: RequestLedger | None = None) -> tuple[_Context, RequestLedger]:
    ledger = ledger or RequestLedger()
    context = _Context()
    await allowlist.install(context, _target(), ledger)  # type: ignore[arg-type]
    return context, ledger


async def _send(
    context: _Context,
    url: str,
    method: str,
    *,
    navigation: bool = True,
) -> _Route:
    route = _Route(_Request(url, method, navigation=navigation))
    await context.route_handler(route)
    return route


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_a_safe_request_to_an_authorised_origin_goes_through(method: str) -> None:
    context, ledger = await _installed()
    route = await _send(context, PAGE, method)
    assert route.continued and route.aborted is None
    assert ledger.blocked_writes == []
    ledger.raise_if_off_scope()  # nothing to raise


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post", "Delete"])
async def test_a_write_to_an_authorised_origin_is_refused_and_stops_the_run(method: str) -> None:
    context, ledger = await _installed()
    route = await _send(context, PAGE, method)
    assert not route.continued and route.aborted == "blockedbyclient"
    assert len(ledger.blocked_writes) == 1
    with pytest.raises(WriteBlocked):
        ledger.raise_if_off_scope()


@pytest.mark.unit
def test_a_blocked_write_is_a_scope_violation_so_the_orchestrator_refuses_not_retries() -> None:
    assert issubclass(WriteBlocked, OffScopeNavigation)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_error_names_the_method_and_path_but_never_the_query() -> None:
    """A query string can carry a claim number; the error and the ledger are logged."""
    context, ledger = await _installed()
    await _send(context, f"{PAGE}/approve?claim=AB12345678&token=s3cret", "POST")
    error = ledger.off_scope_error()
    assert isinstance(error, WriteBlocked)
    text = str(error) + " ".join(ledger.blocked_writes)
    assert "POST" in text and "/claims/approve" in text
    assert "AB12345678" not in text and "s3cret" not in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_write_inside_the_window_goes_through() -> None:
    context, ledger = await _installed()
    ledger.writes_allowed = True
    route = await _send(context, f"{ORIGIN}/login", "POST")
    assert route.continued and ledger.blocked_writes == []
    ledger.raise_if_off_scope()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_window_lets_exactly_one_write_through() -> None:
    """Logging in is one POST. A second write in the same window -- a click that lands on
    'Delete' after the sign-in -- is refused."""
    context, ledger = await _installed()
    ledger.writes_allowed = True
    await _send(context, f"{ORIGIN}/login", "POST")
    second = await _send(context, f"{ORIGIN}/claims/7/delete", "POST")
    assert second.aborted == "blockedbyclient" and not second.continued
    with pytest.raises(WriteBlocked):
        ledger.raise_if_off_scope()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_refused_subresource_write_is_aborted_and_recorded_but_does_not_stop_the_run() -> (
    None
):
    """An analytics beacon or a POST-based search XHR is not a navigation. It is refused -- the
    portal never receives it -- and recorded, but one of them must not fail every run on a
    portal that fires them."""
    context, ledger = await _installed()
    route = await _send(context, f"{ORIGIN}/beacon", "POST", navigation=False)
    assert route.aborted == "blockedbyclient" and not route.continued
    assert ledger.blocked_writes == [f"POST {ORIGIN}/beacon"]
    ledger.raise_if_off_scope()  # still running


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_refused_document_write_stops_the_run_even_after_a_refused_beacon() -> None:
    context, ledger = await _installed()
    await _send(context, f"{ORIGIN}/beacon", "POST", navigation=False)
    await _send(context, f"{ORIGIN}/claims/7/delete", "POST")  # a form submit
    with pytest.raises(WriteBlocked, match="delete"):
        ledger.raise_if_off_scope()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_write_to_another_origin_is_an_off_scope_refusal_even_inside_the_window() -> None:
    context, ledger = await _installed()
    ledger.writes_allowed = True
    route = await _send(context, "https://elsewhere.example.invalid/login", "POST")
    assert route.aborted == "blockedbyclient" and ledger.blocked_writes == []
    assert ledger.blocked_urls  # recorded as an off-origin refusal, not a write attempt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_redirect_hop_that_writes_outside_the_window_is_recorded() -> None:
    """A 307/308 keeps the method. The route handler never sees the hop; the request listener
    does, and cannot abort it -- so it must at least stop the run."""
    context, ledger = await _installed()
    context.request_handler(_Request(f"{ORIGIN}/after", "POST", redirected=True))
    with pytest.raises(WriteBlocked):
        ledger.raise_if_off_scope()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_redirect_hop_that_writes_after_the_sign_in_stops_the_run() -> None:
    """The sign-in spent the window, so a 307/308 hop of that POST is a second write. It cannot
    be aborted (see allowlist._route_write), but it is recorded and the run stops on its next
    operation."""
    context, ledger = await _installed()
    ledger.writes_allowed = True
    await _send(context, f"{ORIGIN}/login", "POST")
    context.request_handler(_Request(f"{ORIGIN}/claims/7/delete", "POST", redirected=True))
    assert ledger.blocked_writes == [f"POST {ORIGIN}/claims/7/delete"]
    with pytest.raises(WriteBlocked):
        ledger.raise_if_off_scope()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_script_write_inside_the_window_is_refused_and_does_not_spend_it() -> None:
    """A heartbeat, a beacon or a hostile page's fetch() fires while the sign-in click is
    running. It must not be the one write the window lets through: the sign-in POST that
    follows still has to get in."""
    context, ledger = await _installed()
    ledger.writes_allowed = True
    script = await _send(context, f"{ORIGIN}/heartbeat", "POST", navigation=False)
    assert script.aborted == "blockedbyclient" and not script.continued
    sign_in = await _send(context, f"{ORIGIN}/login", "POST")
    assert sign_in.continued and sign_in.aborted is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
async def test_only_a_post_may_use_the_window(method: str) -> None:
    """A sign-in form submits with POST. A page whose 'sign in' goes out as DELETE is not one."""
    context, ledger = await _installed()
    ledger.writes_allowed = True
    route = await _send(context, f"{ORIGIN}/login", method)
    assert route.aborted == "blockedbyclient" and not route.continued
    with pytest.raises(WriteBlocked):
        ledger.raise_if_off_scope()


class _ClickPage:
    def get_by_role(self, role: str, *, name: str, exact: bool) -> Any:
        async def click() -> None:
            self.window_during_click = self.ledger.writes_allowed

        return SimpleNamespace(click=click)

    async def wait_for_load_state(self, state: str) -> None:
        return None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_window_opens_only_after_the_rate_limit_token_and_closes_after_the_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token can block for a long time while the login page's scripts are live. The window
    must not be open for that wait, only for the click."""
    ledger = RequestLedger()
    page = _ClickPage()
    page.ledger, page.window_during_click = ledger, None  # type: ignore[attr-defined]
    seen_by_token: list[bool] = []
    monkeypatch.setattr(
        "selector_drift.browser.ratelimit.acquire",
        lambda t: seen_by_token.append(ledger.writes_allowed),
    )
    handle = PageHandle(page, _target(), PAGE, ledger)  # type: ignore[arg-type]
    await handle.click_role("button", "Sign in", signs_in=True)
    assert seen_by_token == [False]
    assert page.window_during_click is True  # type: ignore[attr-defined]
    assert ledger.writes_allowed is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_window_closes_even_when_the_click_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = RequestLedger()

    class _Boom(_ClickPage):
        def get_by_role(self, role: str, *, name: str, exact: bool) -> Any:
            async def click() -> None:
                raise RuntimeError("the click failed")

            return SimpleNamespace(click=click)

    monkeypatch.setattr("selector_drift.browser.ratelimit.acquire", lambda t: None)
    handle = PageHandle(_Boom(), _target(), PAGE, ledger)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await handle.click_role("button", "Sign in", signs_in=True)
    assert ledger.writes_allowed is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_click_that_does_not_sign_in_never_opens_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = RequestLedger()
    page = _ClickPage()
    page.ledger, page.window_during_click = ledger, None  # type: ignore[attr-defined]
    monkeypatch.setattr("selector_drift.browser.ratelimit.acquire", lambda t: None)
    await PageHandle(page, _target(), PAGE, ledger).click_role("button", "Approve")  # type: ignore[arg-type]
    assert page.window_during_click is False  # type: ignore[attr-defined]


@pytest.mark.unit
def test_only_the_navigate_steps_ask_for_the_window() -> None:
    """The window is the one place a write may pass. A heal, an extractor, a rung or the
    pipeline asking for it would reintroduce the hole, so `signs_in=` has exactly one home --
    the code that runs the spec's navigate steps, on the click after a credential fill."""
    root = Path(__file__).resolve().parents[2] / "selector_drift"
    callers = sorted(
        str(p.relative_to(root).as_posix())
        for p in root.rglob("*.py")
        if any(
            isinstance(n, ast.keyword) and n.arg == "signs_in"
            for n in ast.walk(ast.parse(p.read_text("utf-8")))
        )
    )
    assert callers == ["browser/navigate.py"]


class _CdpSession:
    """Just enough of a Playwright CDPSession: records what the guard sends."""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def send(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.sent.append((method, params or {}))


class _GuardedContext:
    def __init__(self) -> None:
        self.pages: list[Any] = []
        self.sessions: list[_CdpSession] = []

    def on(self, event: str, handler: Any) -> None:
        pass

    async def new_cdp_session(self, page: Any) -> _CdpSession:
        self.sessions.append(_CdpSession())
        return self.sessions[-1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_paused_request_the_guard_cannot_judge_is_failed_not_left_hanging() -> None:
    """If deciding raises, the request must not stay paused for ever (the page would hang until
    the run times out) and must not be waved through: it is failed, closed."""
    context = _GuardedContext()
    guard = await allowlist.guard_redirects(context, RequestLedger())  # type: ignore[arg-type]
    await guard(object())  # type: ignore[arg-type]
    session = context.sessions[0]
    session.handlers["Fetch.requestPaused"]({"requestId": "r1"})  # no "request": cannot judge
    await asyncio.sleep(0.05)
    assert ("Fetch.failRequest", {"requestId": "r1", "errorReason": "BlockedByClient"}) in (
        session.sent
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_guard_is_active_once_awaited_and_attaches_once_per_page() -> None:
    """The driver awaits the guard before the page's first navigation, so Fetch is enabled
    before any request exists; the 'page' event for the same page must not attach a second."""
    context = _GuardedContext()
    guard = await allowlist.guard_redirects(context, RequestLedger())  # type: ignore[arg-type]
    page = object()
    await guard(page)  # type: ignore[arg-type]
    await guard(page)  # type: ignore[arg-type]
    assert len(context.sessions) == 1
    assert [m for m, _ in context.sessions[0].sent] == ["Fetch.enable"]
