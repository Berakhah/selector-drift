"""A run that lost most of its rows is held before it lands.

The per-field checks judge the values in the rows a run returned; none of them can see rows that
are missing. A page-1-only pagination failure, a virtualised table or a row scope that shrank
returns correctly placed, correctly formatted rows, so every field check passes and the run
lands incomplete. This judges the run's size against the trailing median of accepted runs.

It runs on a cache hit as much as on a heal: the markup is untouched when a row scope silently
shrinks, so rung 1 still matches. `quality.contracts` applies the same threshold after the write
(it needs the lake); the constants are defined here, once, and imported there.
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING, Final

from selector_drift.extract.models import CandidateExtraction
from selector_drift.validate.assertions.distribution import REFERENCE_RUNS
from selector_drift.validate.history import History
from selector_drift.validate.verdict import Outcome, Verdict

if TYPE_CHECKING:
    from selector_drift.validate.gate import HealContext

__all__ = ["REFERENCE_RUNS", "ROW_COUNT_MIN_WINDOWS", "ROW_COUNT_RATIO", "RowCountContinuity"]

ROW_COUNT_RATIO: Final = 0.5
ROW_COUNT_MIN_WINDOWS: Final = 3


class RowCountContinuity:
    name = "RowCountContinuity"
    threshold = f"at least {ROW_COUNT_RATIO:g} of the trailing median row count"

    def __init__(self, warmup_runs: int = ROW_COUNT_MIN_WINDOWS) -> None:
        """`warmup_runs` is the scope record's: how much history the target needs before it is
        judged. A run's size needs no more than the shared minimum, but a target that declares a
        shorter warm-up is judged sooner, as the distributional check is."""
        self.warmup_runs = warmup_runs
        self.min_runs = max(1, min(ROW_COUNT_MIN_WINDOWS, warmup_runs))

    def _v(self, outcome: Outcome, observed: str | None, reason: str) -> Verdict:
        return Verdict(
            check=self.name,
            outcome=outcome,
            threshold=self.threshold,
            observed=observed,
            reason=reason,
        )

    def check(
        self, candidate: CandidateExtraction, history: History | None, ctx: HealContext
    ) -> Verdict:
        counts = [len(run.rows) for run in history.trailing(REFERENCE_RUNS).runs] if history else []
        if len(counts) < self.min_runs:
            return self._v(
                Outcome.INSUFFICIENT,
                None,
                f"{len(counts)} accepted run(s) of history; no judgement of a run's size "
                f"before {self.min_runs}",
            )
        median = float(statistics.median(counts))
        bar = ROW_COUNT_RATIO * median
        n = len(candidate.rows)
        observed = f"{n} rows against a trailing median of {median:g} over {len(counts)} runs"
        if n >= bar:
            return self._v(Outcome.PASS, observed, "the run is not short of its history")
        return self._v(
            Outcome.FAIL,
            observed,
            f"fewer than {bar:g} rows: rows were lost (a pagination change, a virtualised "
            "table or a row scope that shrank), not a quiet day; the median, not the mean, "
            "so one month-end spike does not move the bar",
        )
