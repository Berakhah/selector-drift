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
_ORIGIN: Final = re.compile(
    r"(?P<scheme>https?)://(?P<host>\[[^\]]*\]|[^:/\[\]]+)(?::(?P<port>\d+))?"
)
_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def canonical_host(host: str) -> str | None:
    """The host as it appears in an origin, or None if it is not a strict host. IPv6 must
    arrive bracketed and leaves bracketed and compressed."""
    if host.startswith("[") and host.endswith("]"):
        try:
            return f"[{ipaddress.IPv6Address(host[1:-1]).compressed}]"
        except ValueError:
            return None
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        pass
    if len(host) > 253:
        return None
    labels = host.split(".")
    if all(_LABEL.fullmatch(label) for label in labels):
        return host
    return None


def _port(raw: str | None, scheme: str) -> int | None:
    if raw is None:
        return _DEFAULT_PORTS[scheme]
    port = int(raw)
    return port if 1 <= port <= 65535 else None


def parse_origin(value: str) -> str:
    """Validate an authored origin; return it canonicalised as `scheme://host:port` with the
    default port explicit. Raises `ValueError` for anything else."""
    match = _ORIGIN.fullmatch(value)
    if match is None:
        raise ValueError(f"origin must be exactly scheme://host[:port], got {value!r}")
    scheme = match["scheme"]
    host = canonical_host(match["host"])
    if host is None or match["host"] != match["host"].lower():
        raise ValueError(f"origin host is not a strict lower-case host: {value!r}")
    port = _port(match["port"], scheme)
    if port is None:
        raise ValueError(f"origin port must be 1-65535: {value!r}")
    return f"{scheme}://{host}:{port}"


def origin_of_url(url: str) -> str | None:
    """The canonical origin of a request URL, or None if the URL is not an absolute http(s) URL
    with a strict host and no userinfo -- which is never in scope. Raw whitespace and control
    characters are refused before parsing, because `urlsplit` silently strips some of them."""
    if not url or any(c.isspace() or ord(c) < 0x20 for c in url):
        return None
    try:
        parts = urlsplit(url)
        raw_port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or "@" in parts.netloc or parts.hostname is None:
        return None
    hostname = parts.hostname  # lower-cased; IPv6 brackets stripped
    host = canonical_host(f"[{hostname}]" if ":" in hostname else hostname)
    if host is None:
        return None
    port = _DEFAULT_PORTS[scheme] if raw_port is None else raw_port
    if not 1 <= port <= 65535:
        return None
    return f"{scheme}://{host}:{port}"
