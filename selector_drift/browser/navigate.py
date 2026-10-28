"""Executes a spec's navigate steps. The only code that drives a page from config.

Every step goes through a PageHandle method, never the raw Playwright page: `goto_step` resolves
a path against the registry URL and checks the origin, `click_role` and `goto_step` each take
the target's rate-limit token themselves (ruling, Important 5 -- this module takes none, so no
navigation waits twice), and `fill_field` addresses a plain form field name, never a selector.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from selector_drift.credentials.provider import Credentials
from selector_drift.spec.schema import NavStep


class NavError(Exception):
    """A navigate step that cannot run as written. The run fails; it never guesses.

    Messages name the step and the credential reference, never a credential's value."""


class Navigable(Protocol):
    """What run_steps needs of a page: PageHandle's three config-driven operations."""

    async def goto_step(self, step: dict[str, object]) -> None: ...

    async def fill_field(self, field: str, value: str) -> None: ...

    async def click_role(self, role: str, name: str, *, signs_in: bool = False) -> None: ...


def _value(i: int, ref: str | None, creds: Credentials | None) -> str:
    if creds is None:
        raise NavError(f"step {i}: this step needs credentials and the target has none")
    if ref == "credentials.username" and creds.username is not None:
        return creds.username
    if ref == "credentials.password" and creds.password is not None:
        return creds.password.get_secret_value()  # never logged, never stored
    raise NavError(f"step {i}: the credentials hold no {ref}")


async def run_steps(page: Navigable, steps: Sequence[NavStep], creds: Credentials | None) -> None:
    """Never-rule 7: nothing here may write into the portal -- except signing in, which is a
    POST. So the one click that follows a credential fill runs in the browser's write window
    (one write, closed afterwards); every other step, a cookie-injected session's included, has
    none. The window is spent by that click: a later 'Approve' gets no window."""
    signing_in = False
    for i, step in enumerate(steps):
        match step.step:
            case "goto":
                await page.goto_step({"step": "goto", "path": step.path})
            case "fill":
                if step.field is None:
                    raise NavError(f"step {i}: fill needs a plain field name")
                await page.fill_field(step.field, _value(i, step.value_from, creds))
                signing_in = True
            case "click":
                if step.role is None or step.name is None:
                    raise NavError(f"step {i}: click needs a role and an accessible name")
                await page.click_role(step.role, step.name, signs_in=signing_in)
                signing_in = False
