"""Never-rule 7 against the real bench portal: the login (a POST) works in the window run_steps
opens around the sign-in click, and a POST anywhere else is refused.

tests/browser/test_write_block.py pins the policy over fake Playwright objects. This runs it for
real: Chromium, the portal's `/login` form, the spec's own navigate steps. The existing
end-to-end pipeline tests are the third leg -- they log in through `run_steps` and would
all fail if the window did not open.

Callers: none (pytest collects tests/orchestrate/test_*.py). Instruction: "test it with the
existing bench".
"""

from __future__ import annotations

import pytest

from selector_drift.browser import driver
from selector_drift.browser.allowlist import WriteBlocked
from selector_drift.browser.driver import _playwright_page
from selector_drift.browser.navigate import run_steps
from selector_drift.scope.gate import authorise
from selector_drift.spec.loader import load_scope, load_spec, scope_file, spec_file
from tests.orchestrate.conftest import Bench, _DemoAccount

_POST_TO_LOGIN = "() => fetch('/login', {method: 'POST'}).then(() => 'sent', () => 'refused')"


def _target_and_steps(bench: Bench):
    spec = load_spec(spec_file("testbench.yaml"))
    target = authorise(load_scope(scope_file(spec.scope_ref)), today=bench.day)
    return target, spec.navigate, _DemoAccount().get(target)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_spec_logs_in_through_the_window_and_a_post_after_it_is_refused(
    bench: Bench,
) -> None:
    target, steps, creds = _target_and_steps(bench)
    async with driver.open(target, creds) as session:
        await run_steps(session.page, steps, creds)
        assert session.page.url.endswith("/results"), session.page.url  # the POST went through

        outcome = await _playwright_page(session.page).evaluate(_POST_TO_LOGIN)
        assert outcome == "refused"  # the browser never sent it
        assert len(session.blocked_writes) == 1 and "POST" in session.blocked_writes[0]
        assert "/login" in session.blocked_writes[0]
        await session.page.content()  # a script's POST is refused, not fatal: it is no navigation
        assert session.blocked_urls == []  # a write attempt, not an off-origin one


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_sign_in_form_submitted_outside_the_window_is_refused_and_stops_the_run(
    bench: Bench,
) -> None:
    """The window is what lets the login through, and run_steps opens it only around the click
    that follows a credential fill. Fill and click through the page handle -- no window -- and
    the form's POST is a refused document write."""
    target, _, creds = _target_and_steps(bench)
    assert creds is not None and creds.username is not None and creds.password is not None
    async with driver.open(target, creds) as session:
        await session.page.goto_step({"step": "goto", "path": "/login"})
        await session.page.fill_field("username", creds.username)
        await session.page.fill_field("password", creds.password.get_secret_value())
        with pytest.raises(WriteBlocked) as refused:
            await session.page.click_role("button", "Sign in")
        assert "POST" in str(refused.value) and "/login" in str(refused.value)
        assert not session.page.url.endswith("/results")
