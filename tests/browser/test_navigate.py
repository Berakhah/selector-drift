"""browser.navigate.run_steps: the only code that drives a page from config (Task 26A).

Unit tests over a recording stand-in for PageHandle; the click/fill methods themselves are
exercised against a fake Playwright page below, and end to end by the pipeline tests."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from selector_drift.browser import ratelimit
from selector_drift.browser.driver import PageHandle
from selector_drift.browser.navigate import NavError, run_steps
from selector_drift.credentials.provider import Credentials
from selector_drift.spec.schema import NavStep

STEPS = [
    NavStep(step="goto", path="/login"),
    NavStep(step="fill", field="username", value_from="credentials.username"),
    NavStep(step="fill", field="password", value_from="credentials.password"),
    NavStep(step="click", role="button", name="Sign in"),
    NavStep(step="goto", path="/results"),
]
DEMO = Credentials(username="demo", password=SecretStr("not-a-real-password"))


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def goto_step(self, step: dict[str, object]) -> None:
        self.calls.append(("goto", str(step["path"])))

    async def fill_field(self, field: str, value: str) -> None:
        self.calls.append(("fill", field, value))

    async def click_role(self, role: str, name: str, *, signs_in: bool = False) -> None:
        self.calls.append(("click", role, name, "signs_in") if signs_in else ("click", role, name))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_steps_run_in_order_through_the_page_handle() -> None:
    page = Recorder()
    await run_steps(page, STEPS, DEMO)  # type: ignore[arg-type]
    assert page.calls == [
        ("goto", "/login"),
        ("fill", "username", "demo"),
        ("fill", "password", "not-a-real-password"),
        ("click", "button", "Sign in", "signs_in"),
        ("goto", "/results"),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_write_window_opens_around_the_sign_in_click_and_nothing_else() -> None:
    """Never-rule 7: logging in is a POST, so the click that follows the credential fills may
    write. A later click -- 'Approve', say -- is outside the window."""
    steps = [*STEPS, NavStep(step="click", role="button", name="Approve")]
    page = Recorder()
    await run_steps(page, steps, DEMO)  # type: ignore[arg-type]
    clicks = [c for c in page.calls if c[0] == "click"]
    assert clicks == [
        ("click", "button", "Sign in", "signs_in"),
        ("click", "button", "Approve"),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_click_nothing_was_filled_for_never_gets_the_window() -> None:
    """A cookie-injected session has no sign-in form, so its spec has no credential fill and no
    click of its own may write."""
    steps = [
        NavStep(step="goto", path="/results"),
        NavStep(step="click", role="button", name="Delete"),
    ]
    page = Recorder()
    await run_steps(page, steps, None)  # type: ignore[arg-type]
    assert page.calls[-1] == ("click", "button", "Delete")  # no `signs_in`


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_fill_step_without_credentials_fails_the_run_rather_than_guessing() -> None:
    with pytest.raises(NavError, match="credentials"):
        await run_steps(Recorder(), STEPS, None)  # type: ignore[arg-type]
    with pytest.raises(NavError, match=r"credentials\.password"):
        await run_steps(Recorder(), STEPS, Credentials(username="demo"))  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_nav_error_never_carries_the_secret() -> None:
    creds = Credentials(username=None, password=SecretStr("hunter2-secret"))
    with pytest.raises(NavError) as info:
        await run_steps(Recorder(), STEPS, creds)  # type: ignore[arg-type]
    assert "hunter2-secret" not in str(info.value)


@pytest.mark.unit
def test_navigate_takes_no_rate_limit_token_itself() -> None:
    """Ruling (Important 5): PageHandle.goto_step and click_role own the acquire. A second
    acquire here would double every wait and block the event loop."""
    tree = ast.parse(Path("selector_drift/browser/navigate.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "ratelimit" not in names and "acquire" not in attrs


class _FakeLocator:
    def __init__(self, log: list[tuple[str, ...]], what: tuple[str, ...]) -> None:
        self._log, self._what = log, what

    async def click(self) -> None:
        self._log.append(("click", *self._what))

    async def fill(self, value: str) -> None:
        self._log.append(("fill", *self._what, value))


class _FakePage:
    def __init__(self) -> None:
        self.log: list[tuple[str, ...]] = []

    def get_by_role(self, role: str, *, name: str, exact: bool) -> _FakeLocator:
        assert exact is True
        return _FakeLocator(self.log, (role, name))

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self.log, (selector,))

    async def wait_for_load_state(self, state: str) -> None:
        self.log.append(("load", state))


class _Ledger:
    def raise_if_off_scope(self) -> None:
        return None

    def off_scope_error(self) -> None:
        return None


def _handle(page: _FakePage) -> PageHandle:
    return PageHandle(page, object(), "http://127.0.0.1:1/", _Ledger())  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_click_role_takes_exactly_one_rate_limit_token(monkeypatch: Any) -> None:
    taken: list[object] = []
    monkeypatch.setattr(ratelimit, "acquire", lambda target: taken.append(target))
    page = _FakePage()
    await _handle(page).click_role("button", "Sign in")
    assert len(taken) == 1
    assert page.log == [("click", "button", "Sign in"), ("load", "load")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fill_field_addresses_a_plain_name_and_takes_no_token(monkeypatch: Any) -> None:
    taken: list[object] = []
    monkeypatch.setattr(ratelimit, "acquire", lambda target: taken.append(target))
    page = _FakePage()
    await _handle(page).fill_field("username", "demo")
    assert taken == [] and page.log == [("fill", "[name='username']", "demo")]
    for bad in ("user']", "a b", "", "x" * 65):
        with pytest.raises(ValueError, match="plain form field name"):
            await _handle(page).fill_field(bad, "demo")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_click_role_refuses_a_role_that_is_not_an_aria_role_name() -> None:
    with pytest.raises(ValueError, match="role"):
        await _handle(_FakePage()).click_role("button[name=x]", "Sign in")
