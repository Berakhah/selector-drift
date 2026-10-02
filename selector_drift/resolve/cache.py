"""The selector cache, READ side only. The write-back is orchestrate/cache_writer.py (C2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.models import SelectorCacheEntry
from selector_drift.db.session import Session
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


class SelectorCache:
    """Postgres-backed, read only. spec_version=None reads the highest version held -- replay
    and tests only; the cascade always passes the run's version (M14)."""

    def __init__(self, session_factory: Callable[[], OrmSession] = Session) -> None:
        self._sessions = session_factory

    def get(
        self, target: str, field: str, spec_version: int | None = None
    ) -> CachedSelector | None:
        query = select(SelectorCacheEntry).where(
            SelectorCacheEntry.target == target, SelectorCacheEntry.field == field
        )
        if spec_version is not None:
            query = query.where(SelectorCacheEntry.spec_version == spec_version)
        query = query.order_by(SelectorCacheEntry.spec_version.desc()).limit(1)
        with self._sessions() as s:
            row = s.scalars(query).first()
            if row is None:
                return None
            return CachedSelector(
                selector=row.selector,
                anchor=AnchorEvidence.model_validate(row.anchor),
                set_by_rung=row.set_by_rung,
            )
