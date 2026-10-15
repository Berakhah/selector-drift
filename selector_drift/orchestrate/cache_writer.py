"""The self-healing write-back. The whole of it. Callable only with the gate's output (C2).

It lives in orchestrate/ because it must import validate/ (for the type) and resolve/ (for
the element), and the judged-cannot-call-judge contract forbids resolve -> validate. Its one
caller is orchestrate.accept.accept() -- the single call site of the three acceptance writes.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.models import SelectorCacheEntry
from selector_drift.db.session import Session
from selector_drift.orchestrate.run_record import parse_run_key
from selector_drift.resolve.trace import ResolvedElement
from selector_drift.validate.gate import ValidatedExtraction


@dataclass(frozen=True)
class _Snapshot:
    """What a cache row held before one put, so that put can be compensated."""

    selector: str
    anchor: dict[str, Any]
    node_id: str | None
    set_by_rung: int
    set_at: dt.datetime
    set_by_run_key: str

    @classmethod
    def of(cls, row: SelectorCacheEntry) -> _Snapshot:
        return cls(
            row.selector, dict(row.anchor), row.node_id, row.set_by_rung, row.set_at,
            row.set_by_run_key,
        )  # fmt: skip

    def restore(self, row: SelectorCacheEntry) -> None:
        row.selector, row.anchor, row.node_id = self.selector, self.anchor, self.node_id
        row.set_by_rung, row.set_at, row.set_by_run_key = (
            self.set_by_rung, self.set_at, self.set_by_run_key,
        )  # fmt: skip


class CacheWriter:
    """Bound to one (target, spec_version): the cache's key minus the field (M14)."""

    def __init__(
        self, *, target: str, spec_version: int, session_factory: Callable[[], OrmSession] = Session
    ) -> None:
        self.target, self.spec_version, self._sessions = target, spec_version, session_factory

    @classmethod
    def for_run_key(cls, run_key: str) -> CacheWriter:
        target, spec_version, _window = parse_run_key(run_key)  # Task 6's run_key() format
        return cls(target=target, spec_version=spec_version)

    def put(
        self, validated: ValidatedExtraction, element: ResolvedElement, *, field: str
    ) -> Callable[[], None]:
        """Write one field's entry and return the compensation that puts back what was there.

        The compensation is compare-and-swap: it acts only while the row is still the one THIS
        call wrote (same run key and stamp), so it can never clobber a later, legitimate heal."""
        if not isinstance(validated, ValidatedExtraction):  # mypy is the first line
            raise TypeError("the cache is written only from a ValidatedExtraction (C2)")
        # Added in 26A: the validated run must be a run of THIS writer's target and spec
        # version, so one target's accepted run can never write another target's selectors.
        target, version, _window = parse_run_key(validated.run_key)
        if target != self.target:
            raise ValueError(
                f"validated run {validated.run_key} is for target {target!r}, not {self.target!r}"
            )
        if version != self.spec_version:
            raise ValueError(
                f"validated run {validated.run_key} is spec version {version}, "
                f"not {self.spec_version}"
            )
        key = (self.target, field, self.spec_version)
        stamp = dt.datetime.now(dt.UTC)
        with self._sessions() as s:
            prior = s.get(SelectorCacheEntry, key, with_for_update=True)
            before = _Snapshot.of(prior) if prior is not None else None
            s.merge(
                SelectorCacheEntry(
                    target=self.target,
                    field=field,
                    spec_version=self.spec_version,
                    selector=element.selector,
                    anchor=element.anchor.model_dump(mode="json"),
                    node_id=element.node_id,
                    set_by_rung=element.rung_id,
                    set_at=stamp,
                    set_by_run_key=validated.run_key,
                )
            )
            s.commit()

        def revert() -> None:
            with self._sessions() as s:
                row = s.get(SelectorCacheEntry, key, with_for_update=True)
                if row is None or row.set_by_run_key != validated.run_key or row.set_at != stamp:
                    return  # someone else owns the row now: not ours to undo
                if before is None:
                    s.delete(row)
                else:
                    before.restore(row)
                s.commit()

        return revert
