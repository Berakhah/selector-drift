"""PSI with a documented threshold, per-target warm-up, and persistence for ALERTING only
(design 5.7, M16/M17/M18). The statistic itself is validate/stats.py, shared with Task 29.

A breach is PSI over max(threshold, critical), where critical is the chi-square noise floor
(validate/stats.critical_psi at `alpha`): over a small sample, PSI over 0.20 is sampling noise.

- Healed: a single-run breach is FAIL; no breach is PASS. No alert.
- Steady state (cache hit): always PASS -- a cache hit's distribution is not a write gate (5.7).
  An alert only when the breach persists `persistence` runs and the client has not marked it
  expected.

"No basis yet" (warm-up, a reference window or current sample too small to be evidence) is
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
    PSI_BINS,
    critical_psi,
    numeric_reference,
    psi_result,
    quantile_edges,
)
from selector_drift.validate.verdict import Outcome, Verdict

if TYPE_CHECKING:
    from selector_drift.scope.record import AuthorisedTarget
    from selector_drift.validate.gate import HealContext

REFERENCE_RUNS: Final = 30
# Set from measured power (fix round 3): a PASS from a check with no power is fail-open. The
# smallest current n at which a x10 shift is detected >= 95% of the time (bench-like data,
# reference 30 x 50 values, 1000 draws) was 35 (98.8%), rounded up to a multiple of 10. At 40:
# 99.8% detected, 0.5% false breach. Below this on EITHER side the check has no basis:
# INSUFFICIENT -- a heal quarantines, a cache hit is tolerated, no alert. docs/methodology.md.
MIN_VALUES: Final = 40
__all__ = ["ALPHA", "MIN_VALUES", "REFERENCE_RUNS", "Assessment", "DistributionalAssertion"]


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
        if (
            not math.isfinite(threshold)
            or threshold <= 0
            or warmup_runs < 1
            or persistence < 1
            or bins < 2
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
        runs = 0 if history is None else len(history)
        if history is None or runs < self.warmup_runs:
            return f"warm-up: {runs} of {self.warmup_runs} accepted runs"
        reference = history.trailing(REFERENCE_RUNS).values(field)
        current = column(candidate, field)
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
        r = psi_result(reference, current, bins=self.bins)
        # k == 1: every value on both sides in the same bin or category -- a constant field that
        # stayed constant. PSI is 0 and there is no noise to floor: no shift, not "no basis"
        # (fix round 2). Different constants are k == 2 and take the normal rule.
        critical = critical_psi(r.k, r.reference_n, r.current_n, self.alpha) if r.k >= 2 else 0.0
        breach = r.psi > max(self.threshold, critical)
        prior = 0
        for run in reversed(history.runs):
            if field not in run.shift_alerts:
                break
            prior += 1
        previous = history.previous()
        suppressed = previous is not None and field in previous.marked_expected
        return Assessment(
            psi=r.psi,
            reference_n=r.reference_n,
            current_n=r.current_n,
            categorical=r.categorical,
            critical=critical,
            breach=breach,
            prior_breaches=prior,
            suppressed=suppressed,
            alert=breach and not suppressed and 1 + prior >= self.persistence,
        )

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
