"""Format assertion: the spec's pattern against every extracted value, through the one matcher."""

from __future__ import annotations

from typing import TYPE_CHECKING

from selector_drift.extract.models import CandidateExtraction
from selector_drift.spec.schema import (
    MATCH_TIMEOUT_S,
    MAX_MATCH_LEN,
    compile_pattern,
    match_value,
)
from selector_drift.validate.assertions.base import column, preview
from selector_drift.validate.history import History
from selector_drift.validate.verdict import Outcome, Verdict

if TYPE_CHECKING:
    from selector_drift.validate.gate import HealContext


class FormatAssertion:
    name = "FormatAssertion"

    def __init__(self, pattern: str) -> None:
        compile_pattern(pattern)  # a broken pattern refuses the spec (SpecError)
        self.pattern = pattern

    def _v(self, outcome: Outcome, observed: str | None, reason: str) -> Verdict:
        return Verdict(
            check=self.name,
            outcome=outcome,
            threshold=self.pattern,
            observed=observed,
            reason=reason,
        )

    def check(
        self, candidate: CandidateExtraction, history: History | None, ctx: HealContext
    ) -> Verdict:
        values = column(candidate, ctx.target.name)
        if not values:
            return self._v(
                Outcome.INSUFFICIENT, None, f"no values were extracted for {ctx.target.name}"
            )
        results = [(v, match_value(self.pattern, v)) for v in values]
        too_long = [v for v, r in results if r == "too_long"]
        if too_long:
            return self._v(
                Outcome.FAIL,
                f"length {max(len(v) for v in too_long)}",
                f"{len(too_long)} value(s) exceed the {MAX_MATCH_LEN}-character "
                f"length cap: a value that long is not a {ctx.target.name}, and "
                "matching it would expose the run to catastrophic backtracking",
            )
        timed_out = [v for v, r in results if r == "timeout"]
        if timed_out:
            return self._v(
                Outcome.FAIL,
                preview(timed_out),
                f"the pattern timed out after {MATCH_TIMEOUT_S * 1000:.0f} ms on "
                f"{len(timed_out)} value(s); a pattern that backtracks is refused, not waited for",
            )
        bad = [v for v, r in results if r != "match"]  # PASS only on an explicit match
        if bad:
            return self._v(
                Outcome.FAIL,
                preview(bad),
                f"{len(bad)} of {len(values)} values do not match the spec's format",
            )
        return self._v(
            Outcome.PASS,
            f"{len(values)} of {len(values)} values match",
            "every value matches the spec's format",
        )
