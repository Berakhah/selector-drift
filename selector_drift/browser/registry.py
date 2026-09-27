"""The only place a URL string lives. A target with no entry has no URL, so it cannot be opened."""

from __future__ import annotations

from typing import Final

_URLS: Final[dict[str, str]] = {
    "testbench": "https://<BENCH-HOST>/",
    "agent-perimeter": "https://<CONTROL-1-HOST>/",
    "ledger-sense": "https://<CONTROL-2-HOST>/",
}


class UnknownTarget(LookupError):
    pass


def url_for(target: str) -> str:
    try:
        return _URLS[target]
    except KeyError:
        raise UnknownTarget(f"no URL registered for target {target!r}") from None
