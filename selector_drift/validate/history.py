"""What we trusted, not what we saw. Built by orchestrate.history.history_for (Task 26A)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AcceptedRun:
    """A run whose rows were WRITTEN (gate passed, or a human promoted it). Quarantined and
    discarded runs never appear."""

    run_key: str
    window: dt.date
    rows: tuple[Mapping[str, str], ...]  # field -> raw extracted string
    natural_key: tuple[str, ...]
    shift_alerts: frozenset[str] = frozenset()  # fields whose PSI breached on this run
    marked_expected: frozenset[str] = frozenset()  # fields a client marked "expected" (B7)

    def by_key(self) -> dict[tuple[str, ...], Mapping[str, str]]:
        return {
            tuple(r[k] for k in self.natural_key): r
            for r in self.rows
            if all(k in r for k in self.natural_key)
        }


@dataclass(frozen=True)
class History:
    runs: tuple[AcceptedRun, ...] = field(default_factory=tuple)  # oldest first

    def previous(self) -> AcceptedRun | None:
        return self.runs[-1] if self.runs else None

    def values(self, field_name: str) -> list[str]:
        return [r[field_name] for run in self.runs for r in run.rows if field_name in r]

    def trailing(self, n: int) -> History:
        return History(runs=self.runs[-n:]) if n > 0 else History()

    def __len__(self) -> int:
        return len(self.runs)
