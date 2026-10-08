"""Design 5.8: the first heal to a new selector waits for a human, unless the scope record
says otherwise. The policy is injected from AuthorisedTarget by checks_for (Task 25)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from selector_drift.extract.models import CandidateExtraction
from selector_drift.scope.record import FirstHealPolicy
from selector_drift.validate.assertions.base import cells
from selector_drift.validate.history import History
from selector_drift.validate.verdict import Outcome, Verdict

if TYPE_CHECKING:
    from selector_drift.validate.gate import HealContext

__all__ = ["FirstHealConfirmation"]


class FirstHealConfirmation:
    name = "FirstHealConfirmation"
    threshold = "a human confirms the first heal to a new selector"

    def __init__(self, policy: FirstHealPolicy = FirstHealPolicy.REQUIRED) -> None:
        self.policy = policy

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
        if not ctx.healed:
            return self._v(
                Outcome.PASS, None, "nothing healed: no first-heal confirmation is needed"
            )
        if ctx.human_confirmed:
            return self._v(Outcome.PASS, None, "a human confirmed this heal at promotion")
        if self.policy is FirstHealPolicy.AUTO_PROMOTE:
            return self._v(
                Outcome.PASS,
                None,
                "the scope record sets first_heal_policy: auto_promote (a synthetic "
                "target with a known answer key, design 5.8)",
            )
        field = ctx.target.name
        field_cells = cells(candidate, field)
        if not field_cells:
            return self._v(Outcome.INSUFFICIENT, None, f"no extracted {field} value to judge")
        if any(c.selector is None for c in field_cells):
            return self._v(
                Outcome.INSUFFICIENT,
                None,
                f"an extracted {field} value does not carry the selector it healed to",
            )
        selectors = sorted({c.selector for c in field_cells if c.selector is not None})
        if len(selectors) > 1:
            return self._v(
                Outcome.INSUFFICIENT,
                None,
                f"{field} values came from {len(selectors)} distinct selectors; one cell "
                "matching the cache cannot vouch for the rest, so a human confirms",
            )
        selector = selectors[0]
        target = candidate.run_key.split(":", 1)[0]
        cached = ctx.cache.get(target, field, candidate.spec_version)
        if cached is not None and cached.selector == selector:
            return self._v(
                Outcome.PASS,
                selector,
                "a heal to a selector already confirmed for this field runs automatically",
            )
        return self._v(
            Outcome.INSUFFICIENT,
            selector,
            f"first heal of {field} to a new selector; the scope record's "
            "first_heal_policy is required, so a human confirms it before its data is written",
        )
