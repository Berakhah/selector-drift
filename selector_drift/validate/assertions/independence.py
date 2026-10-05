"""Cross-family confirmation (design 5.6) and the VALUE family (design 5.6a, R14/C3).

Reaches rungs ONLY through the Rung protocol in resolve.trace (judge-reaches-rungs-only-by-
protocol): the concrete rungs are built in orchestrate/ and injected. Agreement is decided on
the node_id the PageSource minted, never on selector text (R10/C5). Reasons name rungs, node
paths, run keys and rates -- never a cell's text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

from selector_drift.extract.models import CandidateExtraction
from selector_drift.resolve.trace import Family, ResolvedElement, Rung
from selector_drift.validate.history import AcceptedRun, History
from selector_drift.validate.verdict import Confirmation, Outcome, Verdict

if TYPE_CHECKING:
    from selector_drift.resolve.page import PageSource
    from selector_drift.spec.schema import FieldTarget
    from selector_drift.validate.gate import HealContext

_DOM_FAMILIES: Final = frozenset({Family.TEXT_SEMANTIC, Family.STRUCTURAL})
_HEALING_RUNGS: Final = frozenset({2, 3, 4})  # rung 1 is a cache hit; 5 is an escalation
VALUE_CONFIRMATION: Final = Confirmation(rung=None, family=Family.VALUE)
COLLAPSE_FLOOR: Final = 0.50
"""Steady-state agreement below this is a collapse, not churn: FAIL even on a cache hit
(decided 2026-09-26, ledger #32 -- it is what catches ValueSwap under unchanged markup).
A published methodology number (Task 40), not a tunable."""
MIN_JOINED: Final = 10
"""Fewer joined cells than this is no basis for continuity either way: one coincidental match
is not a confirmation, and one legitimate change among two rows is not a collapse (fix round 1)."""


def _norm(value: object) -> str:
    return " ".join(str(value).split())


def continuity_alert(v: Verdict) -> bool:
    """A steady-state continuity dip the gate tolerates (INSUFFICIENT) but a human must hear
    about: the verdict carries an observed rate below its threshold. Only the steady-state path
    produces an INSUFFICIENT with a rate -- on a heal the same rate is a FAIL."""
    if v.check != RowKeyContinuity.name or v.outcome is not Outcome.INSUFFICIENT:
        return False
    if v.observed is None or v.threshold is None:
        return False
    try:
        return float(v.observed) < float(v.threshold.removeprefix(">= "))
    except ValueError:
        return False


class RowKeyContinuity:
    """The VALUE family: join this run to the previous accepted run on the natural key and
    require the field to agree. Reads the data, not the page, so it is independent of both DOM
    families -- and the only signal that sees ValueSwap, where every DOM signal agrees."""

    name = "RowKeyContinuity"

    def __init__(
        self,
        threshold: float = 0.95,
        collapse_floor: float = COLLAPSE_FLOOR,
        min_joined: int = MIN_JOINED,
    ) -> None:
        if min_joined < 1:
            raise ValueError("min_joined must be at least 1")
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        if not 0.0 < collapse_floor < threshold:
            raise ValueError("collapse_floor must be in (0, threshold)")
        self.threshold = threshold
        self.collapse_floor = collapse_floor
        self.min_joined = min_joined

    @property
    def threshold_text(self) -> str:
        return f">= {self.threshold:.2f}"

    def _v(
        self,
        outcome: Outcome,
        observed: str | None,
        reason: str,
        confirmed_by: Confirmation | None = None,
    ) -> Verdict:
        return Verdict(
            check=self.name,
            outcome=outcome,
            threshold=self.threshold_text,
            observed=observed,
            reason=reason,
            confirmed_by=confirmed_by,
        )

    def agreement(
        self, candidate: CandidateExtraction, previous: AcceptedRun, field: str
    ) -> tuple[float, int]:
        """(agreement rate, cells compared). For the key field itself, the co-varying fields.

        Keys and values are compared whitespace-normalised on both sides. A row lacking a key
        part or the field is skipped -- summary and header rows exist (never assume row 0)."""
        key = previous.natural_key
        before: dict[tuple[str, ...], Mapping[str, str]] = {
            tuple(_norm(part) for part in k): row for k, row in previous.by_key().items()
        }
        agree = compared = 0
        for row in candidate.rows:
            if not all(k in row for k in key):
                continue
            old = before.get(tuple(_norm(row[k].value) for k in key))
            if old is None:
                continue
            if field in key:
                fields = [f for f in row if f not in key and f in old]
            else:
                fields = [field] if field in row and field in old else []
            for f in fields:
                compared += 1
                agree += _norm(row[f].value) == _norm(old[f])
        return (agree / compared if compared else 0.0), compared

    def check(
        self, candidate: CandidateExtraction, history: History | None, ctx: HealContext
    ) -> Verdict:
        field = ctx.target.name
        previous = history.previous() if history is not None else None
        if previous is None:
            return self._v(Outcome.INSUFFICIENT, None, "no accepted run to compare against")
        rate, compared = self.agreement(candidate, previous, field)
        joined = "+".join(previous.natural_key)
        if compared == 0:
            return self._v(
                Outcome.INSUFFICIENT,
                None,
                f"no natural keys in common with {previous.run_key} (joined on {joined}), so "
                "continuity has no basis",
            )
        if compared < self.min_joined:
            # No rate is reported: a rate over this few cells is noise, and an observed rate is
            # what continuity_alert reads (fix round 1).
            return self._v(
                Outcome.INSUFFICIENT,
                None,
                f"only {compared} joined cells with {previous.run_key} (joined on {joined}); "
                f"continuity needs at least {self.min_joined}, so it has no basis either way",
            )
        what = "the fields that co-vary with it" if field in previous.natural_key else field
        detail = (
            f"{what} agree with {previous.run_key} on {rate:.1%} of {compared} joined cells "
            f"(joined on {joined})"
        )
        observed = f"{rate:.4f}"
        if not ctx.healed:  # steady state (ledger #32)
            if rate < self.collapse_floor:
                return self._v(
                    Outcome.FAIL,
                    observed,
                    f"{detail}: below the {self.collapse_floor:.2f} collapse floor on a cache "
                    "hit -- no overnight business change moves that many values while every "
                    "key stays; the column reads different data",
                )
            if rate < self.threshold:
                return self._v(
                    Outcome.INSUFFICIENT,
                    observed,
                    f"{detail}: below the threshold but above the collapse floor -- tolerated "
                    "on a cache hit as churn, and reported for review",
                )
            return self._v(
                Outcome.INSUFFICIENT, observed, f"nothing healed, so nothing to confirm; {detail}"
            )
        if rate >= self.threshold:
            return self._v(Outcome.PASS, observed, detail, VALUE_CONFIRMATION)
        return self._v(
            Outcome.FAIL,
            observed,
            f"{detail}: the values moved while the keys did not -- the heal reads a different "
            "column",
        )


class _RungRaised(Exception):
    def __init__(self, rung: Rung, exc: Exception) -> None:
        super().__init__()
        self.rung, self.exc = rung, exc


def _resolve(rung: Rung, page: PageSource, target: FieldTarget) -> ResolvedElement | None:
    try:
        return rung.resolve(page, target)
    except Exception as exc:
        raise _RungRaised(rung, exc) from exc


class IndependentRungAgreement:
    """Re-derive the healed node with the healing rung, re-resolve with every cross-family DOM
    rung, compare node_id. Any cross-family disagreement is a FAIL, even if another confirmer
    agrees. Only when no DOM confirmer resolved does the VALUE family decide."""

    name = "IndependentRungAgreement"
    threshold = "the same node, re-resolved by a different signal family"

    def __init__(self, rungs: Sequence[Rung], value: RowKeyContinuity | None = None) -> None:
        self.rungs = tuple(rungs)
        ids = [r.rung_id for r in self.rungs]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(
                "each rung may be injected once; duplicated: "
                + ", ".join(f"rung {i}" for i in duplicated)
            )
        self.value = value if value is not None else RowKeyContinuity()

    def _v(
        self,
        outcome: Outcome,
        observed: str | None,
        reason: str,
        confirmed_by: Confirmation | None = None,
    ) -> Verdict:
        return Verdict(
            check=self.name,
            outcome=outcome,
            threshold=self.threshold,
            observed=observed,
            reason=reason,
            confirmed_by=confirmed_by,
        )

    def _dom(self, ctx: HealContext) -> Verdict | str:
        """A decisive DOM verdict, or the reason there is no DOM evidence."""
        if ctx.page is None:
            return "no page snapshot to re-resolve against"
        healer = next((r for r in self.rungs if r.rung_id == ctx.winning_rung), None)
        if healer is None:
            # A wiring fault, not an absence of evidence: never let values paper over it
            # (fix round 1, M2).
            return self._v(
                Outcome.INSUFFICIENT,
                None,
                f"no DOM evidence: the healing rung was not injected (rung {ctx.winning_rung}), "
                "so its node cannot be re-derived and the heal is not confirmed",
            )
        if healer.family not in _DOM_FAMILIES:
            return (
                f"rung {healer.rung_id} ({healer.family.value}) healed, and a rung outside the "
                "DOM families has no cross-family counterpart"
            )
        confirmers = sorted(
            (
                r
                for r in self.rungs
                if r.rung_id not in (1, healer.rung_id)
                and r.family in _DOM_FAMILIES
                and r.family is not healer.family
            ),
            key=lambda r: r.rung_id,
        )
        if not confirmers:
            offered = ", ".join(f"rung {r.rung_id} ({r.family.value})" for r in self.rungs)
            return (
                f"no cross-family confirmer available: rung {healer.rung_id} "
                f"({healer.family.value}) healed; offered {offered or 'none'}"
            )
        try:
            return self._compare(ctx.page, ctx.target, healer, confirmers)
        except _RungRaised as raised:
            return self._v(
                Outcome.FAIL,
                f"rung {raised.rung.rung_id} raised",
                f"rung {raised.rung.rung_id} raised {type(raised.exc).__name__} while "
                "re-resolving on this snapshot, so agreement cannot be established",
            )

    def _compare(
        self, page: PageSource, target: FieldTarget, healer: Rung, confirmers: Sequence[Rung]
    ) -> Verdict | str:
        healed = _resolve(healer, page, target)
        if healed is None:
            return self._v(
                Outcome.FAIL,
                f"rung {healer.rung_id} -> nothing",
                f"rung {healer.rung_id} healed this field but does not resolve it again on the "
                "same snapshot: the heal is not reproducible",
            )
        resolved = [(c, el) for c in confirmers if (el := _resolve(c, page, target)) is not None]
        against = [(c, el) for c, el in resolved if el.node_id != healed.node_id]
        if against:
            c, el = against[0]
            return self._v(
                Outcome.FAIL,
                f"rung {c.rung_id} -> {el.node_id}; rung {healer.rung_id} -> {healed.node_id}",
                f"cross-family disagreement: rung {c.rung_id} ({c.family.value}) resolves a "
                f"different node than the heal by rung {healer.rung_id} "
                f"({healer.family.value}) -- the adjacent-column signature",
            )
        if resolved:
            c, _ = resolved[0]
            return self._v(
                Outcome.PASS,
                f"rung {c.rung_id}",
                f"rung {c.rung_id} ({c.family.value}) resolved the same node "
                f"({healed.node_id}) as the heal by rung {healer.rung_id} "
                f"({healer.family.value})",
                Confirmation(rung=c.rung_id, family=c.family),
            )
        tried = ", ".join(f"rung {c.rung_id}" for c in confirmers)
        return f"no cross-family confirmer resolved ({tried} returned nothing)"

    def check(
        self, candidate: CandidateExtraction, history: History | None, ctx: HealContext
    ) -> Verdict:
        if not ctx.healed:
            return self._v(
                Outcome.INSUFFICIENT,
                None,
                "nothing healed: rung 1 verified the cached selector and its anchor, so there "
                "is nothing to confirm",
            )
        if ctx.winning_rung not in _HEALING_RUNGS:
            return self._v(
                Outcome.INSUFFICIENT,
                None,
                f"rung {ctx.winning_rung} is not a deterministic rung: the field escalated, "
                "there is no node to confirm, and values alone never confirm an escalation",
            )
        dom = self._dom(ctx)
        if isinstance(dom, Verdict):
            return dom
        value = self.value.check(candidate, history, ctx)
        if value.outcome is Outcome.PASS:
            return self._v(
                Outcome.PASS,
                "row-key continuity",
                f"{dom}; confirmed by the VALUE family instead: {value.reason}",
                VALUE_CONFIRMATION,
            )
        if value.outcome is Outcome.FAIL:
            return self._v(
                Outcome.FAIL, value.observed, f"{dom}; the VALUE family refuses: {value.reason}"
            )
        return self._v(
            Outcome.INSUFFICIENT,
            None,
            f"{dom}; the VALUE family has no basis either: {value.reason}",
        )
