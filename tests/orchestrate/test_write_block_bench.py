"""Never-rule 7 against the real bench portal: the login (a POST) works in the window, and a POST
anywhere else is refused.

tests/browser/test_write_block.py pins the policy over fake Playwright objects. This runs it for
real: Chromium, the portal's `/login` form, the spec's own navigate steps. The existing
end-to-end pipeline tests are the third leg -- they log in through `submitting_forms()` and would
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
        async with session.page.submitting_forms():
            await run_steps(session.page, steps, creds)
        assert session.page.url.endswith("/results"), session.page.url  # the POST went through

        outcome = await _playwright_page(session.page).evaluate(_POST_TO_LOGIN)
        assert outcome == "refused"  # the browser never sent it
        with pytest.raises(WriteBlocked):
            await session.page.content()
        assert session.blocked_urls == []  # a write attempt, not an off-origin one


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_same_login_is_refused_when_nothing_opened_the_window(bench: Bench) -> None:
    """Proves the window is what lets the login through, and that a navigate step cannot write
    on its own authority."""
    target, steps, creds = _target_and_steps(bench)
    async with driver.open(target, creds) as session:
        with pytest.raises(WriteBlocked) as refused:
            await run_steps(session.page, steps, creds)
        assert "POST" in str(refused.value) and "/login" in str(refused.value)
        assert not session.page.url.endswith("/results")
