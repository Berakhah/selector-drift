"""The selector cache, READ side only. The write-back is orchestrate/cache_writer.py (C2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from selector_drift.resolve.trace import AnchorEvidence


@dataclass(frozen=True)
class CachedSelector:
    selector: str
    anchor: AnchorEvidence  # the evidence that justified the selector (design 5.2)
    set_by_rung: int


class CacheReader(Protocol):
    """What rung 1 reads. SelectorCache satisfies it; so does replay's HealTimeCache."""

    def get(
        self, target: str, field: str, spec_version: int | None = None
    ) -> CachedSelector | None: ...


class _NoCache:
    def get(
        self, target: str, field: str, spec_version: int | None = None
    ) -> CachedSelector | None:
        return None


NO_CACHE: CacheReader = _NoCache()
