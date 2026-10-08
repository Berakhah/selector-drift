"""Session injection -- the default offer (design 2.1 Q2). The client logs in, exports a
Playwright storage state, and we replay its cookies. We never see a password or a second factor.

A cookie is a credential: the state file must be owner-only (POSIX -- the product runs in a
Linux container), only cookies for the target's authorised origins are handed over (H2), a
session with no live cookie is refused rather than run logged out, and only counts are logged.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from selector_drift.credentials.provider import (
    Credentials,
    CredentialsUnavailable,
    NotSupported,
    select_cookies,
    utc_now,
)
from selector_drift.scope.record import AuthorisedTarget

log = logging.getLogger(__name__)

_GROUP_OR_OTHER = stat.S_IRWXG | stat.S_IRWXO


class SessionInjection:
    def __init__(self, storage_state_path: Path, *, now: Callable[[], datetime] = utc_now) -> None:
        self.storage_state_path = storage_state_path
        self._now = now

    def __repr__(self) -> str:
        return f"SessionInjection({self.storage_state_path.name!r})"

    def _read(self) -> list[Any]:
        p = self.storage_state_path
        if not p.is_file():
            raise CredentialsUnavailable(f"no session state at {p.name}; ask the client for one")
        # POSIX only, explicitly: Windows has no mode bits to read. On POSIX this always runs.
        if os.name == "posix" and p.stat().st_mode & _GROUP_OR_OTHER:
            raise CredentialsUnavailable(
                f"{p.name} is readable by group or others; chmod 600 it before use"
            )
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CredentialsUnavailable(f"{p.name} is not a Playwright storage state") from None
        cookies = data.get("cookies", []) if isinstance(data, dict) else None
        if not isinstance(cookies, list):
            raise CredentialsUnavailable(f"{p.name} is not a Playwright storage state")
        return cookies

    def get(self, target: AuthorisedTarget) -> Credentials:
        cookies = self._read()
        picked = select_cookies(cookies, target.origins, self._now())
        if picked.unreadable:
            raise CredentialsUnavailable(
                f"{target.target}: {picked.unreadable} cookie(s) in "
                f"{self.storage_state_path.name} have an unreadable expiry -- this is not a "
                "Playwright storage state"
            )
        if not picked.live:
            raise CredentialsUnavailable(
                f"{target.target}: the injected session has no live cookie for the authorised "
                "origin -- ask the client for a fresh session"
            )
        # Counts only, never a name or a value.
        log.info(
            "session injection for %s: %d live cookie(s); %d dropped as off-origin, %d expired",
            target.target,
            len(picked.live),
            picked.off_origin,
            picked.expired,
        )
        return Credentials(cookies=picked.live)

    def get_totp_code(self, target: AuthorisedTarget) -> str:
        raise NotSupported(
            "session injection replays a session; it holds no second factor -- "
            "the client completes MFA when they export the session"
        )
