"""The self-healing write-back. The whole of it. Callable only with the gate's output (C2).

It lives in orchestrate/ because it must import validate/ (for the type) and resolve/ (for
the element), and the judged-cannot-call-judge contract forbids resolve -> validate. Its one
caller is orchestrate.accept.accept() -- the single call site of the three acceptance writes.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.models import SelectorCacheEntry
from selector_drift.db.session import Session
from selector_drift.orchestrate.run_record import parse_run_key
from selector_drift.resolve.trace import ResolvedElement
from selector_drift.validate.gate import ValidatedExtraction


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

    def put(self, validated: ValidatedExtraction, element: ResolvedElement, *, field: str) -> None:
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
        with self._sessions() as s:
            s.merge(
                SelectorCacheEntry(
                    target=self.target,
                    field=field,
                    spec_version=self.spec_version,
                    selector=element.selector,
                    anchor=element.anchor.model_dump(mode="json"),
                    node_id=element.node_id,
                    set_by_rung=element.rung_id,
                    set_at=dt.datetime.now(dt.UTC),
                    set_by_run_key=validated.run_key,
                )
            )
            s.commit()
