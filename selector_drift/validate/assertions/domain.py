"""Domain assertion: range, enum membership, date plausibility and currency sanity."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from selector_drift.extract.models import CandidateExtraction
from selector_drift.spec.schema import MAX_MATCH_LEN, DomainRule, SpecError
from selector_drift.validate.assertions._numbers import parse_number
from selector_drift.validate.assertions.base import cells, preview
from selector_drift.validate.history import History
from selector_drift.validate.verdict import Outcome, Verdict

if TYPE_CHECKING:
    from selector_drift.validate.gate import HealContext

# DomainRule is Task 5's TypedDict -- one definition, validated when the spec loads (2026-09-26).
# The checks below stay: a rule built in code, not loaded, must be refused just the same.
_KEYS: Final = frozenset({"min", "max", "enum", "date_after", "date_before", "currency"})
_MINOR_UNITS: Final = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "CAD": 2,
    "AUD": 2,
    "CHF": 2,
    "JPY": 0,
    "KWD": 3,
    "BHD": 3,
}


def _bound(raw: object, key: str) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float | str | Decimal):
        raise SpecError(f"domain rule {key!r} must be a number, got {raw!r}")
    parsed = parse_number(raw) if isinstance(raw, str) else Decimal(str(raw))
    if parsed is None or not parsed.is_finite():
        raise SpecError(f"domain rule {key!r} must be a number, got {raw!r}")
    return parsed


def _day(raw: object, key: str) -> dt.date | None:
    """Task 5's DomainRule carries dates as ISO `YYYY-MM-DD` strings (its IsoDate validator)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            pass
    raise SpecError(f"domain rule {key!r} must be an ISO YYYY-MM-DD string, got {raw!r}")


class DomainAssertion:
    name = "DomainAssertion"

    def __init__(self, rule: DomainRule) -> None:
        if not rule:
            raise SpecError("an empty domain rule checks nothing")
        unknown = set(rule) - _KEYS
        if unknown:
            raise SpecError(f"unknown domain rule key(s) {sorted(unknown)}")
        self._min = _bound(rule.get("min"), "min")
        self._max = _bound(rule.get("max"), "max")
        if self._min is not None and self._max is not None and self._min > self._max:
            raise SpecError(f"domain rule min {self._min} exceeds max {self._max}")
        enum = rule.get("enum")
        if enum is not None and (
            not isinstance(enum, list | tuple) or not all(isinstance(e, str) for e in enum)
        ):
            raise SpecError(f"domain rule 'enum' must be a list of strings, got {enum!r}")
        self._enum = frozenset(enum) if enum is not None else None
        self._after = _day(rule.get("date_after"), "date_after")
        self._before = _day(rule.get("date_before"), "date_before")
        currency = rule.get("currency")
        if currency is not None and (not isinstance(currency, str) or currency not in _MINOR_UNITS):
            raise SpecError(f"unknown currency {currency!r}; known: {sorted(_MINOR_UNITS)}")
        self._currency = currency if isinstance(currency, str) else None
        self._numeric = self._min is not None or self._max is not None or self._currency is not None
        self._dated = self._after is not None or self._before is not None
        self.threshold = self._describe()

    def _describe(self) -> str:
        parts: list[str] = []
        if self._min is not None or self._max is not None:
            lo = f"{self._min} <= " if self._min is not None else ""
            hi = f" <= {self._max}" if self._max is not None else ""
            parts.append(f"{lo}x{hi}")
        if self._currency is not None:
            parts.append(f"{self._currency}, at most {_MINOR_UNITS[self._currency]} decimals")
        if self._enum is not None:
            parts.append("one of {" + ", ".join(sorted(self._enum)) + "}")
        if self._after is not None:
            parts.append(f"after {self._after}")
        if self._before is not None:
            parts.append(f"before {self._before}")
        if self._dated:
            parts.append("not after the day observed")
        return "; ".join(parts)

    def _problem(self, value: str, observed_on: dt.date | None) -> str | None:
        # Reasons never echo a cell (page text); only `observed`, via preview, may.
        if len(value) > MAX_MATCH_LEN:  # the matcher's cap: never parse a page essay
            return f"a value of length {len(value)} exceeds the {MAX_MATCH_LEN}-character cap"
        if self._numeric:
            n = parse_number(value)
            if n is None:
                return "a value is not a number in 1,234.56 form"
            if self._min is not None and n < self._min:
                return f"{n} is below {self._min}"
            if self._max is not None and n > self._max:
                return f"{n} is above {self._max}"
            if self._currency is not None:
                exponent = n.as_tuple().exponent
                if isinstance(exponent, int) and -exponent > _MINOR_UNITS[self._currency]:
                    return f"{n} has more decimal places than {self._currency} allows"
        if self._enum is not None and value not in self._enum:
            return "a value is not an allowed value"
        if self._dated:
            try:
                day = dt.date.fromisoformat(value)
            except ValueError:
                return "a value is not an ISO date"
            if self._after is not None and day <= self._after:
                return f"{day} is not after {self._after}"
            if self._before is not None and day >= self._before:
                return f"{day} is not before {self._before}"
            if observed_on is not None and day > observed_on:
                return f"{day} is after the day the page was observed ({observed_on})"
        return None

    def check(
        self, candidate: CandidateExtraction, history: History | None, ctx: HealContext
    ) -> Verdict:
        found = cells(candidate, ctx.target.name)
        if not found:
            return Verdict(
                check=self.name,
                outcome=Outcome.INSUFFICIENT,
                threshold=self.threshold,
                observed=None,
                reason=f"no values were extracted for {ctx.target.name}",
            )
        problems = [
            (c.value, p)
            for c in found
            if (
                p := self._problem(
                    c.value, c.observed_at.date() if c.observed_at is not None else None
                )
            )
            is not None
        ]
        if problems:
            return Verdict(
                check=self.name,
                outcome=Outcome.FAIL,
                threshold=self.threshold,
                observed=preview([v for v, _ in problems]),
                reason=f"{len(problems)} of {len(found)} values fall outside the "
                f"domain; first: {problems[0][1]}",
            )
        return Verdict(
            check=self.name,
            outcome=Outcome.PASS,
            threshold=self.threshold,
            observed=f"{len(found)} of {len(found)} values in domain",
            reason="every value is inside the spec's domain",
        )
