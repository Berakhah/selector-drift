"""The gate. The only place a ValidatedExtraction is minted (design 3.2)."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn

from selector_drift.extract.models import CandidateExtraction, Row
from selector_drift.spec.schema import FieldTarget
from selector_drift.validate.assertions.base import Assertion
from selector_drift.validate.history import History
from selector_drift.validate.verdict import Outcome, Verdict

if TYPE_CHECKING:
    # Annotations only (2026-09-26). A runtime import made api -> validate.gate -> resolve.cache
    # an indirect import that Task 34's api-is-read-side contract counts; `.importlinter` sets
    # exclude_type_checking_imports = True (Task 34), so these two are not edges in the graph.
    from selector_drift.resolve.cache import CacheReader
    from selector_drift.resolve.page import PageSource

RESOLVING_RUNGS: Final = frozenset({1, 2, 3, 4})


@dataclass(frozen=True)
class HealContext:
    """Everything a check may know about ONE field's resolution.

    `winning_rung` is `trace.winner or 5` (Task 26A): a field no rung resolved arrives as 5 and
    gate_run adds an Escalation FAIL for it. `healed` must be exactly `winning_rung != 1` --
    `CascadeTrace.healed` -- so a heal can never ride the lenient cache-hit path.
    """

    healed: bool
    winning_rung: int
    page: PageSource | None
    target: FieldTarget
    cache: CacheReader
    human_confirmed: bool = False

    def __post_init__(self) -> None:
        if self.healed != (self.winning_rung != 1):
            raise ValueError(
                f"healed={self.healed} contradicts winning_rung={self.winning_rung}: a field "
                "is healed exactly when rung 1 did not win (CascadeTrace.healed)"
            )


class _ValidateToken:
    __slots__ = ()


_TOKEN: Final = _ValidateToken()  # the only instance ValidatedExtraction accepts
# True only inside _construct. The token alone is not enough: dataclasses.replace() re-passes the
# real token from an existing instance, which would mint an altered extraction (fix round 1).
_CONSTRUCTING: ContextVar[bool] = ContextVar("_CONSTRUCTING", default=False)
_NOT_FROM_GATE: Final = "a ValidatedExtraction is minted only by validate.gate (C2)"


@dataclass(frozen=True)
class ValidatedExtraction:
    rows: tuple[Row, ...]
    run_key: str
    verdicts: tuple[Verdict, ...]
    _token: _ValidateToken = dataclasses.field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _TOKEN or not _CONSTRUCTING.get():  # identity, not isinstance (M29)
            raise TypeError(_NOT_FROM_GATE)

    # copy bypasses __init__ (it rebuilds through __reduce_ex__), so it would duplicate a
    # judged extraction without passing __post_init__. A validated run is used, never copied.
    def __copy__(self) -> NoReturn:
        raise TypeError(_NOT_FROM_GATE)

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError(_NOT_FROM_GATE)


def _construct(candidate: CandidateExtraction, verdicts: Sequence[Verdict]) -> ValidatedExtraction:
    """The one mint site."""
    reset = _CONSTRUCTING.set(True)
    try:
        return ValidatedExtraction(
            rows=tuple(candidate.rows),
            run_key=candidate.run_key,
            verdicts=tuple(verdicts),
            _token=_TOKEN,
        )
    finally:
        _CONSTRUCTING.reset(reset)


@dataclass(frozen=True)
class QuarantinedRun:
    """Re-exported by validate.quarantine (Task 25). data_uri is set when orchestrate holds
    the candidate's rows (Task 26A `hold`)."""

    run_key: str
    reason: str
    verdicts: tuple[Verdict, ...]
    data_uri: str = ""


def _effective(v: Verdict, strict: bool) -> Outcome:
    """Design 5.7. `strict` is healed-and-not-yet-human-confirmed."""
    if v.outcome is Outcome.INSUFFICIENT and strict:
        return Outcome.FAIL
    return v.outcome


def _escalated(rung: int) -> Verdict:
    return Verdict(
        check="Escalation",
        outcome=Outcome.FAIL,
        threshold="resolved by rungs 1-4",
        observed=f"rung {rung}",
        reason="no deterministic rung resolved this field; rung 5 escalates to a "
        "human and an escalated field never writes (design 5.3, 5.7)",
    )


def _name_of(check: Assertion) -> str:
    """A check's name for its verdict; the class name when it has no usable one."""
    name = getattr(check, "name", None)
    return name if isinstance(name, str) and name else type(check).__name__


def _judge(
    check: Assertion, candidate: CandidateExtraction, history: History | None, ctx: HealContext
) -> Verdict:
    """Every check yields a Verdict. A crash would lose the run's data, and a non-Verdict
    (None, a bare boolean) is no judgement at all: both become a FAIL kept in quarantine."""
    try:
        verdict: object = check.check(candidate, history, ctx)
    except Exception as exc:
        return Verdict(
            check=_name_of(check),
            outcome=Outcome.FAIL,
            threshold="the check returns a verdict",
            observed=type(exc).__name__,
            reason=f"the check raised instead of judging: {exc!r}",
        )
    if not isinstance(verdict, Verdict):
        return Verdict(
            check=_name_of(check),
            outcome=Outcome.FAIL,
            threshold="the check returns a verdict",
            observed=type(verdict).__name__,
            reason="the check returned something other than a Verdict, which is not a judgement",
        )
    return verdict


def _require_every_field_judged(
    candidate: CandidateExtraction,
    contexts: Mapping[str, HealContext],
    checks: Mapping[str, Sequence[Assertion]],
) -> None:
    """Fail closed (fix round 1). The mint carries every row whole, so every field in any row
    -- not just row 0 -- or in the trace must have been judged; and a check filed under a
    field with no context would otherwise be silently skipped."""
    if not contexts:
        raise ValueError("a run with no fields has nothing to judge")
    for name, ctx in contexts.items():
        if ctx.target.name != name:
            raise ValueError(
                f"contexts[{name!r}] carries the target {ctx.target.name!r}: a context must be "
                "filed under its own field"
            )
    present = {f for row in candidate.rows for f in row} | set(candidate.trace)
    unjudged = sorted(present - contexts.keys())
    if unjudged:
        raise ValueError(
            f"field(s) {', '.join(map(repr, unjudged))} appear in the candidate but have no "
            "context: an unjudged field would be written unjudged"
        )
    orphaned = sorted(checks.keys() - contexts.keys())
    if orphaned:
        raise ValueError(
            f"checks for {', '.join(map(repr, orphaned))} have no context: a check that is "
            "filed but never run is a check skipped"
        )


def gate_run(
    candidate: CandidateExtraction,
    history: History | None,
    contexts: Mapping[str, HealContext],
    checks: Mapping[str, Sequence[Assertion]],
) -> ValidatedExtraction | QuarantinedRun:
    """Judge a whole run, once, over every field; mint once or quarantine once.

    Every check runs on every field, cache hit or heal: the heal flag only decides whether
    INSUFFICIENT is tolerated, never whether a check is consulted (a ValueSwap arrives as a
    rung-1 hit and only a check can see it).
    """
    _require_every_field_judged(candidate, contexts, checks)
    verdicts: list[Verdict] = []
    failing: list[Verdict] = []
    for field_name, ctx in contexts.items():
        field_checks = checks.get(field_name)
        if not field_checks:
            raise ValueError(
                f"field {field_name!r} has no checks: a field judged by nothing is not judged"
            )
        strict = ctx.healed and not ctx.human_confirmed
        judged = [_judge(c, candidate, history, ctx) for c in field_checks]
        if ctx.winning_rung not in RESOLVING_RUNGS:
            judged.append(_escalated(ctx.winning_rung))
        for v in judged:
            stamped = v.model_copy(update={"field": field_name})
            verdicts.append(stamped)
            # FAIL, not "not PASS": INSUFFICIENT is effective-FAIL on the strict path and
            # tolerated off it (design 5.7's table). Any other reading voids the asymmetry.
            if _effective(stamped, strict) is Outcome.FAIL:
                failing.append(stamped)
    if failing:
        names = ", ".join(sorted({f"{v.field}:{v.check}" for v in failing}))
        return QuarantinedRun(
            run_key=candidate.run_key, reason=f"gate failed: {names}", verdicts=tuple(verdicts)
        )
    return _construct(candidate, verdicts)


def gate(
    candidate: CandidateExtraction,
    history: History | None,
    ctx: HealContext,
    checks: Sequence[Assertion],
) -> ValidatedExtraction | QuarantinedRun:
    return gate_run(candidate, history, {ctx.target.name: ctx}, {ctx.target.name: checks})
