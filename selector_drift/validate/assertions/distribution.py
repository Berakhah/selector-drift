"""PSI with a documented threshold, per-target warm-up, and persistence for ALERTING only
(design 5.7, M16/M17/M18). The statistic itself is validate/stats.py. Task 29's distribution-shift
rule calls assess_values() -- assess() without the candidate -- so the gate and the quality
screen share every guard and the breach rule.

A breach is PSI over max(threshold, critical), where critical is the chi-square noise floor
(validate/stats.critical_psi at `alpha`): over a small sample, PSI over 0.20 is sampling noise.

- Healed: a single-run breach is FAIL; no breach is PASS. No alert.
- Steady state (cache hit): always PASS -- a cache hit's distribution is not a write gate (5.7).
  An alert only when the breach persists `persistence` runs and the client has not marked it
  expected.

"No basis yet" (warm-up, a reference window or current sample too small to be evidence, a
numeric reference with fewer than MIN_VALUES parseable values, a numeric current run with fewer
than MIN_VALUES non-blank cells, a categorical field with fewer than MIN_EXPECTED_PER_BIN current
values per category) is
INSUFFICIENT, never PASS: on a heal the gate treats INSUFFICIENT as FAIL, on a cache hit it is
tolerated -- exactly the asymmetry the table needs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from selector_drift.extract.models import CandidateExtraction
from selector_drift.validate.assertions.base import column
from selector_drift.validate.history import History
from selector_drift.validate.stats import (
    ALPHA,
    MIN_EXPECTED_PER_BIN,
    PSI_BINS,
    PsiResult,
    critical_psi,
    is_blank,
    numeric_reference,
    psi_result,
    quantile_edges,
)
from selector_drift.validate.verdict import Outcome, Verdict

if TYPE_CHECKING:
    from selector_drift.scope.record import AuthorisedTarget
    from selector_drift.validate.gate import HealContext

REFERENCE_RUNS: Final = 30
# Set from measured power: a PASS from a check with no power is fail-open. MIN_VALUES is the
# smallest current n at which BOTH a x10 and a x3 shift are detected >= 95% of the time, rounded
# up to a multiple of 10, as scripts/measure_psi_power.py measures it through _judge() (bench-like
# data, reference 30 x 50 values, 1000 draws). x10 alone was the rule until the overflow bin
# (fix round 1) made a x10 shift visible from the ~29% of values above the reference max: it gave
# 20, where x3 was caught 49.1% of the time. With both: n=35 x3 93.7%, n=40 x3 98.0% (x10 100%,
# false breach 0.9%), so 40 (fix round 2). Below this on EITHER side -- or below it in parseable
# reference values -- the check has no basis: INSUFFICIENT -- a heal quarantines, a cache hit is
# tolerated, no alert. docs/methodology.md.
MIN_VALUES: Final = 40
__all__ = [
    "ALPHA",
    "MIN_VALUES",
    "REFERENCE_RUNS",
    "Assessment",
    "DistributionalAssertion",
    "Judgement",
]


@dataclass(frozen=True)
class Assessment:
    psi: float
    reference_n: int
    current_n: int
    categorical: bool
    critical: float  # the chi-square noise floor for this run's k and n (stats.critical_psi)
    breach: bool  # PSI over max(threshold, critical) on THIS run -- Task 29 records every breach
    prior_breaches: int  # consecutive trailing accepted runs that breached
    suppressed: bool  # the client marked this field's shift expected on the last run
    alert: bool  # breach, persisted, not suppressed -- Task 29 notifies on this


@dataclass(frozen=True)
class Judgement:
    """The breach rule applied to two value lists (DistributionalAssertion._judge)."""

    result: PsiResult
    critical: float  # 0.0 when k == 1
    breach: bool  # PSI over max(threshold, critical)


class DistributionalAssertion:
    name = "DistributionalAssertion"

    def __init__(
        self,
        threshold: float = 0.20,
        warmup_runs: int = 30,
        persistence: int = 2,
        bins: int = PSI_BINS,
        alpha: float = ALPHA,
    ) -> None:
        # NaN compares False against everything: a NaN threshold would never breach and a NaN
        # alpha would make the critical value NaN. Both disable the check, so both are refused.
        # A threshold over 1 is far past any "significant shift" line, and MIN_VALUES was
        # measured at PSI_BINS bins: more bins is an unmeasured check (review M5).
        if (
            not math.isfinite(threshold)
            or not 0.0 < threshold <= 1.0
            or warmup_runs < 1
            or persistence < 1
            or not 2 <= bins <= PSI_BINS
            or not 0.0 < alpha < 1.0  # NaN fails this too
            or not 1.0 - alpha < 1.0  # an alpha so small that 1 - alpha rounds to 1
        ):
            raise ValueError("these parameters would disable the check, not tune it")
        self.threshold, self.warmup_runs, self.persistence = threshold, warmup_runs, persistence
        self.bins, self.alpha = bins, alpha

    @classmethod
    def for_target(cls, target: AuthorisedTarget) -> DistributionalAssertion:
        return cls(warmup_runs=target.warmup_runs)  # the scope record's value (M18)

    @property
    def threshold_text(self) -> str:
        return f"PSI {self.threshold:.2f}"

    def bin_edges_for(self, history: History, field: str) -> tuple[float, ...]:
        """Quantile edges from the reference window ONLY. The current run is not an input.
        Empty for a text field (the categorical path has no edges)."""
        ref = numeric_reference(history.trailing(REFERENCE_RUNS).values(field))
        return quantile_edges(ref, self.bins) if ref else ()

    def assess(
        self, candidate: CandidateExtraction, history: History | None, field: str
    ) -> Assessment | str:
        """The Assessment, or a str: the reason there is no basis to judge."""
        return self.assess_values(history, column(candidate, field), field)

    def assess_values(
        self, history: History | None, current: list[str], field: str
    ) -> Assessment | str:
        """assess() over a field's current values rather than a candidate: every guard, the
        breach rule, persistence and suppression. Task 29's quality rule reads the current run
        from the lake, and calls this so it never re-implements any of it."""
        runs = 0 if history is None else len(history)
        if history is None or runs < self.warmup_runs:
            return f"warm-up: {runs} of {self.warmup_runs} accepted runs"
        reference = history.trailing(REFERENCE_RUNS).values(field)
        if not reference:
            return f"the reference window holds no {field} values"
        if not current:
            return f"no {field} values were extracted"
        if len(reference) < MIN_VALUES:
            return (
                f"the reference window holds {len(reference)} {field} values, fewer than "
                f"{MIN_VALUES}: too few to be evidence"
            )
        if len(current) < MIN_VALUES:
            return (
                f"{len(current)} {field} values were extracted, fewer than {MIN_VALUES}: "
                "too few to be evidence"
            )
        ref_num = numeric_reference(reference)
        if ref_num is not None and len(ref_num) < MIN_VALUES:
            # Numeric, but mostly blank: quantile edges from a handful of numbers are degenerate
            # (fix round 2) -- no basis, never a verdict.
            return (
                f"the reference window holds {len(ref_num)} parseable {field} values, fewer than "
                f"{MIN_VALUES}: too few to place bin edges"
            )
        if ref_num is not None:
            # The current side counts NON-BLANK cells (fix round 3): ten amounts among ninety
            # blanks are ten values of evidence, not a hundred. Text counts -- a numeric
            # reference is at most 5% text, so text in the current run is itself a shift and
            # cannot dilute power. Counts only, never cell text.
            filled = sum(1 for v in current if not is_blank(v))
            if filled < MIN_VALUES:
                return (
                    f"{filled} non-blank {field} values were extracted, fewer than "
                    f"{MIN_VALUES}: too few to be evidence"
                )
        j = self._judge(reference, current, field)
        if isinstance(j, str):
            return j
        prior = 0
        for run in reversed(history.runs):
            if field not in run.shift_alerts:
                break
            prior += 1
        previous = history.previous()
        suppressed = previous is not None and field in previous.marked_expected
        return Assessment(
            psi=j.result.psi,
            reference_n=j.result.reference_n,
            current_n=j.result.current_n,
            categorical=j.result.categorical,
            critical=j.critical,
            breach=j.breach,
            prior_breaches=prior,
            suppressed=suppressed,
            alert=j.breach and not suppressed and 1 + prior >= self.persistence,
        )

    def _judge(self, reference: list[str], current: list[str], field: str) -> Judgement | str:
        """The breach rule on two non-empty value lists, or a str: the reason there is no basis.
        PRIVATE (fix round 2): it skips warm-up and MIN_VALUES, so a caller reading "no breach"
        as PASS would fail open -- assess_values() (which assess() delegates to) is the only path to
        a verdict: it is this plus its guards. scripts/measure_psi_power.py measures through it
        below MIN_VALUES, so the published power is the product's."""
        r = psi_result(reference, current, bins=self.bins)
        # Categorical k is unbounded (one per distinct value on either side), and the chi-square
        # floor assumes ~MIN_EXPECTED_PER_BIN per bin: under it critical_psi swamps any PSI, so a
        # "PASS" would mean nothing (review C2). Counts only -- never cell text.
        if r.categorical and r.current_n < MIN_EXPECTED_PER_BIN * r.k:
            return (
                f"{field} is compared over {r.k} categories, and {r.current_n} {field} values "
                f"are fewer than {MIN_EXPECTED_PER_BIN} per category: too few to be evidence"
            )
        # k == 1: every value on both sides in the same bin or category -- a constant field that
        # stayed constant. PSI is 0 and there is no noise to floor: no shift, not "no basis"
        # (fix round 2). Different constants are k == 2 and take the normal rule.
        critical = critical_psi(r.k, r.reference_n, r.current_n, self.alpha) if r.k >= 2 else 0.0
        return Judgement(result=r, critical=critical, breach=r.psi > max(self.threshold, critical))

    def check(
        self, candidate: CandidateExtraction, history: History | None, ctx: HealContext
    ) -> Verdict:
        field = ctx.target.name
        a = self.assess(candidate, history, field)
        if isinstance(a, str):
            return Verdict(
                check=self.name,
                outcome=Outcome.INSUFFICIENT,
                threshold=self.threshold_text,
                observed=None,
                reason=a,
            )
        how = (
            "category proportions from"
            if a.categorical
            else f"{self.bins} quantile bins fixed from"
        )
        basis = (
            f"PSI {a.psi:.4f} over {how} the trailing {REFERENCE_RUNS} accepted runs "
            f"(n={a.reference_n} reference, {a.current_n} current); threshold "
            f"{self.threshold:.2f}, critical {a.critical:.4f} (chi-square noise floor at alpha "
            f"{self.alpha:g}); a breach needs PSI over the larger"
        )
        observed = f"{a.psi:.4f}"
        if ctx.healed:
            if a.breach:
                return Verdict(
                    check=self.name,
                    outcome=Outcome.FAIL,
                    threshold=self.threshold_text,
                    observed=observed,
                    reason=f"{basis} breaches on the run the selector healed: a "
                    "heal into a shifted distribution blocks on one run (M17)",
                )
            return Verdict(
                check=self.name,
                outcome=Outcome.PASS,
                threshold=self.threshold_text,
                observed=observed,
                reason=f"distribution unchanged: {basis}",
            )
        if not a.breach:
            reason = f"distribution unchanged: {basis}"
        elif a.suppressed:
            reason = f"{basis}; the client marked this shift expected, so no alert (B7)"
        elif a.alert:
            reason = (
                f"{basis}; breached on {1 + a.prior_breaches} consecutive runs -- a "
                "distribution-shift alert is raised; a cache hit does not block the write"
            )
        else:
            reason = (
                f"{basis}; breach {1 + a.prior_breaches} of persistence {self.persistence} -- "
                "no alert yet (B7)"
            )
        return Verdict(
            check=self.name,
            outcome=Outcome.PASS,
            threshold=self.threshold_text,
            observed=observed,
            reason=reason,
        )
