"""Plain builders for the gate's tests -- data, not fixtures, so every test reads as its inputs.
Tasks 22 and 23 append to this module."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

from bok_core.provenance.claim import Claim, Method
from bok_core.provenance.source import DomSource

from selector_drift.extract.models import CandidateExtraction
from selector_drift.resolve.cache import CachedSelector
from selector_drift.resolve.page import PageSource, StaticPageSource
from selector_drift.resolve.trace import AnchorEvidence, Family, ResolvedElement
from selector_drift.spec.schema import FieldTarget
from selector_drift.validate.gate import HealContext
from selector_drift.validate.history import AcceptedRun, History
from selector_drift.validate.verdict import Outcome, Verdict

AT = dt.datetime(2026, 10, 1, 4, 12, tzinfo=dt.UTC)  # fixed: never the wall clock (M20)
RUN_KEY = "testbench:2:2026-10-01"
PAGE = StaticPageSource("<html><body><p>fixture</p></body></html>")


def field_target(
    name: str, *, assertions: Sequence[Mapping[str, object]] = (), distributional: bool = False
) -> FieldTarget:
    return FieldTarget.model_validate(
        {
            "name": name,
            "description": f"the {name} cell",
            "relation": None,
            "assertions": list(assertions),
            "distributional": distributional,
        }
    )


CLAIM_NUMBER = field_target(
    "claim_number", assertions=[{"type": "format", "pattern": r"^[A-Z]{2}\d{8}$"}]
)
AMOUNT = field_target("amount", distributional=True)
RESERVE = field_target("reserve", distributional=True)
STATUS = field_target("status", distributional=True)


def claim(value: str, *, selector: str = "#healed", rung: int = 4, at: dt.datetime = AT) -> Claim:  # type: ignore[type-arg]
    return Claim(
        value=value,
        method=Method.DETERMINISTIC,
        confidence=None,
        observed_at=at,
        source=DomSource(snapshot_id="0" * 64, selector=selector, rung=rung),
    )


def candidate(
    rows: Sequence[Mapping[str, str]],
    *,
    run_key: str = RUN_KEY,
    selector: str = "#healed",
    at: dt.datetime = AT,
) -> CandidateExtraction:
    return CandidateExtraction(
        rows=[{k: claim(v, selector=selector, at=at) for k, v in r.items()} for r in rows],
        trace={},
        spec_version=2,
        run_key=run_key,
    )


def column(field: str, values: Sequence[str], **kw: object) -> CandidateExtraction:
    return candidate([{field: v} for v in values], **kw)  # type: ignore[arg-type]


class EmptyCache:
    def get(
        self, target: str, field: str, spec_version: int | None = None
    ) -> CachedSelector | None:
        return None


class DictCache:
    """A read-only cache keyed by field, satisfying CacheReader (Task 19)."""

    def __init__(self, entries: Mapping[str, CachedSelector]) -> None:
        self.entries = dict(entries)

    def get(
        self, target: str, field: str, spec_version: int | None = None
    ) -> CachedSelector | None:
        return self.entries.get(field)


def ctx(
    target: FieldTarget = CLAIM_NUMBER,
    *,
    healed: bool = True,
    rung: int = 3,
    page: PageSource | None = PAGE,
    cache: object | None = None,
    confirmed: bool = False,
) -> HealContext:
    return HealContext(
        healed=healed,
        winning_rung=rung,
        page=page,
        target=target,
        cache=cache if cache is not None else EmptyCache(),  # type: ignore[arg-type]
        human_confirmed=confirmed,
    )


class _Fixed:
    outcome: Outcome

    def __init__(self, name: str | None = None) -> None:
        self.name = name or type(self).__name__
        self.calls = 0

    def check(
        self, candidate: CandidateExtraction, history: History | None, ctx: HealContext
    ) -> Verdict:
        self.calls += 1
        return Verdict(
            check=self.name,
            outcome=self.outcome,
            threshold="fixed",
            observed=self.outcome.value,
            reason=f"test double that always returns {self.outcome.value}",
        )


class AlwaysPass(_Fixed):
    outcome = Outcome.PASS


class AlwaysFail(_Fixed):
    outcome = Outcome.FAIL


class AlwaysInsufficient(_Fixed):
    outcome = Outcome.INSUFFICIENT


class Raises:
    name = "Raises"

    def check(
        self, candidate: CandidateExtraction, history: History | None, ctx: HealContext
    ) -> Verdict:
        raise RuntimeError("boom")


# -- Task 22: rung doubles and the row-key fixtures -------------------------------------------


class FakeRung:
    """Satisfies the Rung protocol. validate/ is tested against the protocol only; the real
    rungs against real mutated pages are in test_independence_bench.py and Task 26's suites."""

    def __init__(
        self, rung_id: int, family: Family, node_id: str | None, *, selector: str | None = None
    ) -> None:
        self.rung_id, self.family, self.node_id = rung_id, family, node_id
        self.selector = selector or f"#from-rung-{rung_id}"
        self.calls = 0

    def resolve(self, page: PageSource, target: FieldTarget) -> ResolvedElement | None:
        self.calls += 1
        if self.node_id is None:
            return None
        return ResolvedElement(
            selector=self.selector,
            node_id=self.node_id,
            anchor=AnchorEvidence(accessible_name=None, label_text=None, header_text=None),
            rung_id=self.rung_id,
        )


def ax(node: str | None, **kw: str) -> FakeRung:
    return FakeRung(2, Family.TEXT_SEMANTIC, node, **kw)


def anchors(node: str | None, **kw: str) -> FakeRung:
    return FakeRung(3, Family.TEXT_SEMANTIC, node, **kw)


def heuristic(node: str | None, **kw: str) -> FakeRung:
    return FakeRung(4, Family.STRUCTURAL, node, **kw)


AMOUNT_CELL = "/html/body/table/tr[2]/td[3]"
PRIOR_AMOUNT_CELL = "/html/body/table/tr[2]/td[4]"

BASE_ROWS = [
    {
        "claim_number": f"AB{i:08d}",
        "amount": f"{1000 + 37 * i:,.2f}",
        "reserve": f"{200 + 11 * i:,.2f}",
        "prior_amount": f"{900 + 53 * i:,.2f}",
    }
    for i in range(1, 21)
]
SPEC_FIELDS = ("claim_number", "amount", "reserve")


def _spec_row(r: Mapping[str, str]) -> dict[str, str]:
    return {k: r[k] for k in SPEC_FIELDS}


def previous_history() -> History:
    return History(
        runs=(
            AcceptedRun(
                run_key="testbench:2:2026-09-30",
                window=dt.date(2026, 9, 30),
                rows=tuple(_spec_row(r) for r in BASE_ROWS),
                natural_key=("claim_number",),
            ),
        )
    )


UNCHANGED = candidate([_spec_row(r) for r in BASE_ROWS])  # reword / reorder: same data
DECOY = candidate([_spec_row(r) | {"amount": r["prior_amount"]} for r in BASE_ROWS])
SWAPPED = candidate(
    [_spec_row(r) | {"amount": r["reserve"], "reserve": r["amount"]} for r in BASE_ROWS]
)
FOREIGN_KEYS = candidate(
    [_spec_row(r) | {"claim_number": f"ZZ{i:08d}"} for i, r in enumerate(BASE_ROWS)]
)
# Ordinary overnight churn: 2 of 20 amounts change (agreement 0.90 -- above the collapse floor).
CHURNED = candidate(
    [
        _spec_row(r) | ({"amount": "9,999.99"} if i % 10 == 0 else {})
        for i, r in enumerate(BASE_ROWS)
    ]
)
