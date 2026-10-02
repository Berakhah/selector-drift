"""One resolutions row per scheduled resolution, win or lose (R5/C4)."""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from selector_drift.db.models import Resolution
from selector_drift.db.session import Session
from selector_drift.resolve.trace import CascadeTrace
from selector_drift.spec.schema import FieldTarget


class ResolutionRecorder(Protocol):
    def __call__(
        self,
        *,
        run_key: str,
        portal: str,
        target: FieldTarget,
        trace: CascadeTrace,
        node_id: str | None,
        ms: int,
    ) -> int: ...


def record_resolution(
    *,
    run_key: str,
    portal: str,
    target: FieldTarget,
    trace: CascadeTrace,
    node_id: str | None,
    ms: int,
) -> int:
    """The denominator of every published rung share. Returns the row id (heal_events FK)."""
    with Session() as s:
        row = Resolution(
            run_key=run_key,
            target=portal,
            field=target.name,
            winning_rung=trace.winner,
            attempts=trace.as_json(),
            node_id=node_id,
            ms=ms,
            at=dt.datetime.now(dt.UTC),
        )
        s.add(row)
        s.commit()
        return int(row.id)
