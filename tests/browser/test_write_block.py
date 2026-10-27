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
async def test_the_window_opens_for_the_steps_and_closes_after_them() -> None:
    ledger = RequestLedger()
    handle = PageHandle(None, _target(), PAGE, ledger)  # type: ignore[arg-type]
    assert ledger.writes_allowed is False
    async with handle.submitting_forms():
        assert ledger.writes_allowed is True
    assert ledger.writes_allowed is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_window_closes_even_when_the_steps_fail() -> None:
    ledger = RequestLedger()
    handle = PageHandle(None, _target(), PAGE, ledger)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        async with handle.submitting_forms():
            raise RuntimeError("a navigate step failed")
    assert ledger.writes_allowed is False


@pytest.mark.unit
def test_only_the_navigate_steps_open_the_window() -> None:
    """The window is the one place a write may pass. A heal, an extractor, a rung or the
    pipeline opening it would reintroduce the hole, so the call has exactly one home -- the
    code that runs the spec's navigate steps, which opens it around a sign-in click only."""
    root = Path(__file__).resolve().parents[2] / "selector_drift"
    callers = sorted(
        str(p.relative_to(root).as_posix())
        for p in root.rglob("*.py")
        if any(
            isinstance(n, ast.Attribute) and n.attr == "submitting_forms"
            for n in ast.walk(ast.parse(p.read_text("utf-8")))
        )
        and p.name != "driver.py"
    )
    assert callers == ["browser/navigate.py"]
