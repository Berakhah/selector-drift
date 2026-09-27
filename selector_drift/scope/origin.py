"""The one definition of an origin (I-1).

An origin in a scope record becomes a Chromium proxy-bypass rule (browser/allowlist.py). That
list is comma/semicolon-separated and understands wildcards and `<local>`-style tokens, so a
lax origin widens the egress allowlist: `http://*` becomes the rule `*:80`, and
`http://a.example,*` splits into two rules. Anything that is not exactly `scheme://host[:port]`
is therefore refused -- in the scope record at parse time, and again in every URL the browser
asks for.

- scheme: `http` or `https`, lower-case
- host: a lower-case DNS name (labels `[a-z0-9-]`, not starting or ending with `-`, no
  wildcard), an IPv4 literal, or a bracketed IPv6 literal -- both checked by `ipaddress`
- port: optional, 1-65535
- nothing else: no path (not even `/`), query, fragment, userinfo, whitespace, `,`, `;`, `*`,
  `<` or `>`
"""

from __future__ import annotations

import ipaddress
import re
from typing import Final
from urllib.parse import urlsplit

_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
# Allow-listed by character, ASCII only (re.ASCII; `\d` would match Unicode digits). Bracket
# contents are lower-case hex, `:` and `.` only -- no `%`, so no IPv6 zone id, whose content
# `ipaddress` accepts almost unchecked (`[::1%,*]` was a comma in the proxy bypass list).
_AUTHORITY: Final = re.compile(
    r"(?P<host>\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::(?P<port>[0-9]+))?", re.ASCII
)
_ORIGIN: Final = re.compile(r"(?P<scheme>https?)://(?P<authority>[^/?#]*)", re.ASCII)
_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_NUMERIC_LABEL: Final = re.compile(r"[0-9]+|0x[0-9a-f]*", re.ASCII)


def canonical_host(host: str) -> str | None:
    """The host as it appears in an origin, or None if it is not a strict host.

    - `[...]`: lower-case hex/`:`/`.` only, a valid IPv6 address with no zone id; returned
      bracketed and compressed.
    - numeric-looking (final label all digits, or any label `0x...`): only a canonical
      dotted-quad IPv4 that round-trips through `ipaddress` unchanged -- so `0x7f.1`,
      `2130706433` and `127.000.0.1`, which browsers read as 127.0.0.1, are refused.
    - otherwise a lower-case DNS name: labels `[a-z0-9-]`, no leading/trailing `-`.
    """
    if host.startswith("["):
        inner = host[1:-1]
        if not re.fullmatch(r"\[[0-9a-f:.]+\]", host, re.ASCII):
            return None
        try:
            address = ipaddress.IPv6Address(inner)
        except ValueError:
            return None
        if address.scope_id is not None:
            return None
        return f"[{address.compressed}]"
    if not re.fullmatch(r"[a-z0-9.-]+", host, re.ASCII) or len(host) > 253:
        return None
    labels = host.split(".")
    if _NUMERIC_LABEL.fullmatch(labels[-1]) or any(lb.startswith("0x") for lb in labels):
        try:
            return host if str(ipaddress.IPv4Address(host)) == host else None
        except ValueError:
            return None
    if all(_LABEL.fullmatch(label) for label in labels):
        return host
    return None


def _canonical_authority(authority: str, scheme: str) -> str | None:
    """`host[:port]` -> `host:port`, or None. The port must be ASCII digits, 1-65535, and
    round-trip (no leading zeros: `:080` is refused)."""
    match = _AUTHORITY.fullmatch(authority)
    if match is None:
        return None
    host = canonical_host(match["host"])
    if host is None:
        return None
    raw_port = match["port"]
    if raw_port is None:
        port = _DEFAULT_PORTS[scheme]
    else:
        port = int(raw_port)
        if str(port) != raw_port or not 1 <= port <= 65535:
            return None
    return f"{host}:{port}"


def parse_origin(value: str) -> str:
    """Validate an authored origin; return it canonicalised as `scheme://host:port` with the
    default port explicit. Raises `ValueError` for anything else."""
    match = _ORIGIN.fullmatch(value)
    if match is None:
        raise ValueError(f"origin must be exactly scheme://host[:port], got {value!r}")
    authority = _canonical_authority(match["authority"], match["scheme"])
    if authority is None:
        raise ValueError(f"origin host or port is not strict: {value!r}")
    return f"{match['scheme']}://{authority}"


def origin_of_url(url: str) -> str | None:
    """The canonical origin of a request URL, or None if the URL is not an absolute http(s) URL
    with a strict host and port and no userinfo -- which is never in scope. The authority is
    parsed by the same allow-list as `parse_origin` (after lower-casing, since browsers send
    hosts lower-case anyway), never by `urlsplit(...).port`, which calls `int()` on Unicode
    digits. Raw whitespace and control characters are refused before parsing, because
    `urlsplit` silently strips some of them."""
    if not url or not url.isascii() or any(c.isspace() or ord(c) < 0x20 for c in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    authority = _canonical_authority(parts.netloc.lower(), scheme)
    if authority is None:
        return None
    return f"{scheme}://{authority}"
