"""What a target's credentials are, and the one operation only the client's own broker may
perform (design 2.1 Q2; brief rule 4). No field here, or anywhere, holds a TOTP seed --
tests/credentials/test_no_totp_seed.py inspects every model, dataclass and table to prove it."""

from __future__ import annotations

import ipaddress
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer

from selector_drift.scope.record import AuthorisedTarget

SECRET_MASK = "**********"  # what SecretStr prints


class NotSupported(Exception):
    """This provider cannot do that -- by design, not by omission."""


class CredentialsUnavailable(Exception):
    """No usable credential for the target. The run fails; it never proceeds logged out.
    Messages name files and counts, never a cookie value or a password."""


class Credentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # A session cookie IS the login: excluded from repr (and so from str) like a password, and
    # masked on serialisation (below). In process the attribute holds the real values -- that
    # is what the driver hands to Playwright's add_cookies; nothing else should read it.
    cookies: list[dict[str, Any]] | None = Field(default=None, repr=False)
    username: str | None = None
    password: SecretStr | None = None

    @field_serializer("cookies")
    def _mask_cookie_values(
        self, cookies: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        """M1 (fix round 1): `model_dump()` / `model_dump_json()` mask each cookie's value the
        way SecretStr masks a password, keeping name/domain/path/expiry for diagnostics."""
        if cookies is None:
            return None
        return [{**c, "value": SECRET_MASK} if "value" in c else dict(c) for c in cookies]


@runtime_checkable
class CredentialProvider(Protocol):
    def get(self, target: AuthorisedTarget) -> Credentials: ...

    def get_totp_code(self, target: AuthorisedTarget) -> str:
        """A one-time code, asked of the client's own broker (Task 32). Every other provider
        raises `NotSupported`: nothing in this product can compute one."""
        ...


# -- H2: which cookies may reach the target ----------------------------------------------------
# One implementation for every provider (fix round 1, Task 32): the driver hands
# `Credentials.cookies` to Playwright as-is, so each provider must filter before returning.


def utc_now() -> datetime:
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
    """H2 (Task 31 fix round 1): a matching cookie is never handed over as scoped. Each
    authorised host it matches gets its own copy with `domain=<host>` -- no leading dot, which
    Playwright treats as host-only -- so a parent-domain or public-suffix cookie
    (`.example.invalid`, `com`) can only ever reach the exact authorised host."""
    domain = str(cookie.get("domain", ""))
    return [{**cookie, "domain": h} for h in hosts if _domain_matches(domain, h)]


def _expiry(cookie: dict[str, Any]) -> float | None:
    """The cookie's expiry as a timestamp, -1 for a browser-session cookie, None if unreadable."""
    raw = cookie.get("expires", -1)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class CookieSelection:
    """The cookies that may reach the target, and counts of the rest. Counts are what a caller
    logs; `live` holds values and is kept out of the repr."""

    live: list[dict[str, Any]] = field(repr=False)
    off_origin: int  # entries matching no authorised host (or not a cookie at all)
    expired: int
    unreadable: int  # on-origin cookies whose expiry cannot be read


def select_cookies(
    cookies: Sequence[Any], origins: Iterable[str], now: datetime
) -> CookieSelection:
    """Host-only clones of the cookies that domain-match an authorised origin's host and are
    still live (a session cookie, expiry -1, is live)."""
    hosts = sorted({h.lower() for o in origins if (h := urlsplit(o).hostname)})
    ts = now.timestamp()
    matching = [c for c in cookies if isinstance(c, dict) and _host_only_clones(c, hosts)]
    for_origin = [clone for c in matching for clone in _host_only_clones(c, hosts)]
    live: list[dict[str, Any]] = []
    unreadable = 0
    for c in for_origin:
        expires = _expiry(c)
        if expires is None:
            unreadable += 1
        elif expires < 0 or expires > ts:
            live.append(c)
    return CookieSelection(
        live=live,
        off_origin=len(cookies) - len(matching),
        expired=len(for_origin) - len(live) - unreadable,
        unreadable=unreadable,
    )
