# selector_drift/validate/checks.py
"""A field's declared assertions -> the check objects the gate runs. Confirming rungs are
injected by orchestrate/, so validate/ sees only the Rung protocol."""

from __future__ import annotations

from collections.abc import Sequence

from selector_drift.resolve.trace import Rung  # the protocol, not a rung
from selector_drift.scope.record import FirstHealPolicy
from selector_drift.spec.schema import FieldTarget, SpecError, TaskSpec
from selector_drift.validate.assertions.base import Assertion
from selector_drift.validate.assertions.cross_field import CrossFieldAssertion
from selector_drift.validate.assertions.distribution import DistributionalAssertion
from selector_drift.validate.assertions.domain import DomainAssertion
from selector_drift.validate.assertions.first_heal import FirstHealConfirmation
from selector_drift.validate.assertions.format import FormatAssertion
from selector_drift.validate.assertions.independence import (
    IndependentRungAgreement,
    RowKeyContinuity,
)


def checks_for(
    spec: TaskSpec,
    field: FieldTarget,
    confirmers: Sequence[Rung],
    *,
    warmup_runs: int,
    first_heal_policy: FirstHealPolicy,
) -> list[Assertion]:
    extracted = {t.name for t in spec.targets}
    out: list[Assertion] = []
    for a in field.assertions:
        match a.type:
            case "format":
                if a.pattern is None:
                    raise SpecError(f"format assertion on {field.name} needs a pattern")
                out.append(FormatAssertion(a.pattern))
            case "domain":
                if a.rule is None:
                    raise SpecError(f"domain assertion on {field.name} needs a rule")
                out.append(DomainAssertion(a.rule))
            case "cross_field":
                if a.expression is None:
                    raise SpecError(f"cross_field assertion on {field.name} needs an expression")
                cf = CrossFieldAssertion(a.expression)  # UnsafeExpression is a SpecError
                unknown = cf.names - extracted
                if unknown:
                    raise SpecError(
                        f"cross_field on {field.name} names fields the spec does "
                        f"not extract: {sorted(unknown)}"
                    )
                out.append(cf)
            case other:
                raise SpecError(f"unknown assertion type {other!r} on {field.name}")
    continuity = RowKeyContinuity()  # ONE instance: the embedded and standalone checks
    out.append(IndependentRungAgreement(list(confirmers), value=continuity))  # cannot diverge
    out.append(continuity)
    if field.distributional:
        out.append(DistributionalAssertion(warmup_runs=warmup_runs))
    out.append(FirstHealConfirmation(first_heal_policy))
    return out
