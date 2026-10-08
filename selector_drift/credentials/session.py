"""Session injection -- the default offer (design 2.1 Q2). The client logs in, exports a
Playwright storage state, and we replay its cookies. We never see a password or a second factor.

A cookie is a credential: the state file must be owner-only (POSIX -- the product runs in a
Linux container), only cookies for the target's authorised origins are handed over (H2), a
session with no live cookie is refused rather than run logged out, and only counts are logged.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from selector_drift.credentials.provider import Credentials, CredentialsUnavailable, NotSupported
from selector_drift.scope.record import AuthorisedTarget

log = logging.getLogger(__name__)

_GROUP_OR_OTHER = stat.S_IRWXG | stat.S_IRWXO


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _domain_matches(domain: str, host: str) -> bool:
    """RFC 6265 domain-match: the cookie's domain is the host, or a parent of it. An IP-literal
    host matches only exactly -- `0.0.1` is not a parent of `127.0.0.1`."""
    d = domain.lstrip(".").lower()
    if not d:
        return False
    if _is_ip_literal(host):
        return d == host
    return host == d or host.endswith("." + d)


def _host_only_clones(cookie: dict[str, Any], hosts: list[str]) -> list[dict[str, Any]]:
    """H2 (fix round 1): a matching cookie is never handed over as scoped. Each authorised host
    it matches gets its own copy with `domain=<host>` -- no leading dot, which Playwright treats
    as host-only -- so a parent-domain or public-suffix cookie (`.example.invalid`, `com`) can
    only ever reach the exact authorised host."""
    domain = str(cookie.get("domain", ""))
    return [{**cookie, "domain": h} for h in hosts if _domain_matches(domain, h)]


def _expiry(cookie: dict[str, Any]) -> float | None:
    """The cookie's expiry as a timestamp, -1 for a browser-session cookie, None if unreadable."""
    raw = cookie.get("expires", -1)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


class SessionInjection:
    def __init__(self, storage_state_path: Path, *, now: Callable[[], datetime] = _utc_now) -> None:
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
        hosts = sorted({h.lower() for o in target.origins if (h := urlsplit(o).hostname)})
        now = self._now().timestamp()
        cookies = self._read()
        matching = [c for c in cookies if isinstance(c, dict) and _host_only_clones(c, hosts)]
        for_origin = [clone for c in matching for clone in _host_only_clones(c, hosts)]
        live: list[dict[str, Any]] = []
        unreadable = 0
        for c in for_origin:
            expires = _expiry(c)
            if expires is None:
                unreadable += 1
            elif expires < 0 or expires > now:
                live.append(c)
        if unreadable:
            raise CredentialsUnavailable(
                f"{target.target}: {unreadable} cookie(s) in {self.storage_state_path.name} have "
                "an unreadable expiry -- this is not a Playwright storage state"
            )
        if not live:
            raise CredentialsUnavailable(
                f"{target.target}: the injected session has no live cookie for the authorised "
                "origin -- ask the client for a fresh session"
            )
        # Counts only, never a name or a value.
        log.info(
            "session injection for %s: %d live cookie(s); %d dropped as off-origin, %d expired",
            target.target,
            len(live),
            len(cookies) - len(matching),
            len(for_origin) - len(live),
        )
        return Credentials(cookies=live)

    def get_totp_code(self, target: AuthorisedTarget) -> str:
        raise NotSupported(
            "session injection replays a session; it holds no second factor -- "
            "the client completes MFA when they export the session"
        )
