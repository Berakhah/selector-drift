"""Never-rule 4: a credential is never in an error, a log or a stored run.

Playwright's own failure message for `locator.fill()` carries a call log that echoes the value
(`- fill("<value>")`, reproduced against Chromium on a readonly input). `orchestrate.flows`
stores `str(exc)` in `runs.error`, so a failed password fill would have written the password
to Postgres. `PageHandle.fill_field` must raise something that names the field and nothing else.

Callers: none (pytest collects tests/browser/test_*.py). Instruction: "yes" -- reproduce the
password-in-error case first.
"""

from __future__ import annotations

import traceback
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from selector_drift.browser.allowlist import RequestLedger
from selector_drift.browser.driver import FillFailed, PageHandle
from tests.browser.test_write_block import PAGE, _target

SECRET = "S3cretPassw0rd!"

# What Playwright 1.x actually raised on a readonly input (see the module docstring).
_PLAYWRIGHT_MESSAGE = f"""Locator.fill: Timeout 1500ms exceeded.
Call log:
  - waiting for locator("[name='password']")
    - locator resolved to <input readonly name="password"/>
    - fill("{SECRET}")
  - attempting fill action
    2 x waiting for element to be visible, enabled and editable
      - element is not editable"""


class _FailingLocator:
    async def fill(self, value: str) -> None:
        raise PlaywrightError(_PLAYWRIGHT_MESSAGE)


def _handle() -> PageHandle:
    page: Any = SimpleNamespace(locator=lambda _selector: _FailingLocator())
    return PageHandle(page, _target(), PAGE, RequestLedger())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_fill_never_carries_the_value_in_its_message() -> None:
    with pytest.raises(FillFailed) as raised:
        await _handle().fill_field("password", SECRET)
    assert SECRET not in str(raised.value)
    assert "password" in str(raised.value)  # the field name is plain and useful


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_fill_does_not_chain_the_playwright_error_into_a_traceback() -> None:
    """Prefect and `logging.exception` print the whole chain, cause and context included."""
    with pytest.raises(FillFailed) as raised:
        await _handle().fill_field("password", SECRET)
    rendered = "".join(traceback.format_exception(raised.value))
    assert SECRET not in rendered
