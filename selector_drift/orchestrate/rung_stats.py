"""Share of resolutions won by each rung, over every resolution recorded (R5/C4, B11).

Task 34B replaces the body with a call to derive.rung_shares(); the semantics are fixed here:
keys rung1..rung5, denominator every row in `resolutions` including rows no rung won, and an
empty dict before the first resolution.
"""

from __future__ import annotations

from sqlalchemy import func, select

from selector_drift.db.models import Resolution
from selector_drift.db.session import Session

RUNGS = (1, 2, 3, 4, 5)
SEPARATOR = " · "


def rung_stats() -> dict[str, float]:
    with Session() as s:
        rows = (
            s.execute(
                select(Resolution.winning_rung, func.count()).group_by(Resolution.winning_rung)
            )
            .tuples()
            .all()
        )
    counts = {rung: n for rung, n in rows}
    total = sum(counts.values())
    if total == 0:
        return {}
    return {f"rung{r}": counts.get(r, 0) / total for r in RUNGS}


def format_stats(stats: dict[str, float]) -> str:
    return SEPARATOR.join(f"{k} {v:.1%}" for k, v in stats.items()) or "no resolutions yet"


if __name__ == "__main__":
    print(format_stats(rung_stats()))
