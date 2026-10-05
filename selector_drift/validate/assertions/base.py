"""The Assertion protocol, and the one way every check reads a field out of a candidate."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, NamedTuple, Protocol

from bok_core.provenance.source import DomSource

from selector_drift.extract.models import CandidateExtraction
from selector_drift.validate.history import History
from selector_drift.validate.verdict import Verdict

if TYPE_CHECKING:
    from selector_drift.validate.gate import HealContext


class Assertion(Protocol):
    name: str

    def check(
        self, candidate: CandidateExtraction, history: History | None, ctx: HealContext
    ) -> Verdict: ...


class Cell(NamedTuple):
    value: str
    observed_at: dt.datetime | None
    selector: str | None


def cells(candidate: CandidateExtraction, field: str) -> list[Cell]:
    out: list[Cell] = []
    for row in candidate.rows:
        claim = row.get(field)
        if claim is None:
            continue
        at = claim.observed_at
        source = claim.source
        out.append(
            Cell(
                value=str(claim.value),
                observed_at=at if isinstance(at, dt.datetime) else None,
                selector=source.selector if isinstance(source, DomSource) else None,
            )
        )
    return out


def column(candidate: CandidateExtraction, field: str) -> list[str]:
    return [c.value for c in cells(candidate, field)]


def preview(values: list[str], limit: int = 3, width: int = 40) -> str:
    """At most `limit` values, each at most `width` characters: a verdict names the offending
    value without copying a page into the heal log."""
    shown = ", ".join(repr(v if len(v) <= width else v[:width] + "...") for v in values[:limit])
    more = len(values) - limit
    return shown + (f" (+{more} more)" if more > 0 else "")
