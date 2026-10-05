"""Task 22 against the REAL rungs 2, 3 and 4 on the test bench's mutated pages.

No browser: the bench page is rendered and mutated in-process and read through
StaticPageSource, exactly as tests/resolve does. Tests may import concrete rungs; only
selector_drift.validate may not (judge-reaches-rungs-only-by-protocol).

The real rungs are hardened against DecoyColumn: on the bench page rungs 2, 3 and 4 all
resolve the TRUE Amount column (tests/resolve proves it). So no real rung produces the wrong
heal. The wrong heal the decoy is designed to induce -- a structural heal by remembered column
position -- is played by `StaleColumnRung`, a STRUCTURAL rung-4 stand-in that resolves the
cell at the column index Amount held before the mutation. Every confirmer is real.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest
from lxml import html as lh

from selector_drift.extract.models import CandidateExtraction
from selector_drift.resolve.page import PageSource
from selector_drift.resolve.rungs.r2_axtree import AxTreeRung
from selector_drift.resolve.rungs.r3_anchors import AnchorRung
from selector_drift.resolve.rungs.r4_heuristics import HeuristicRung
from selector_drift.resolve.trace import AnchorEvidence, Family, ResolvedElement, Rung
from selector_drift.spec.schema import FieldTarget
from selector_drift.validate.assertions.base import Assertion
from selector_drift.validate.assertions.independence import (
    IndependentRungAgreement,
    RowKeyContinuity,
)
from selector_drift.validate.gate import (
    HealContext,
    QuarantinedRun,
    ValidatedExtraction,
    gate_run,
)
from selector_drift.validate.history import AcceptedRun, History
from selector_drift.validate.verdict import Confirmation, Outcome
from testbench.mutations.adversarial.decoy_column import DECOY_LABELS
from testbench.portal.render import render_results
from tests.resolve.conftest import AMOUNT, CLAIM_NUMBER, RESERVE, BenchPage
from tests.testbench.helpers import FIXTURE_DAY
from tests.validate.helpers import candidate, ctx

FIELDS = (CLAIM_NUMBER, AMOUNT, RESERVE)
ROWS = "/html/body/main/table/tbody/tr"
AMOUNT_COLUMN_BEFORE = 3  # td index of Amount on the unmutated bench page


@dataclass(frozen=True)
class StaleColumnRung:
    """A naive STRUCTURAL heal: the first record's cell at a remembered column index. This is
    the heal DecoyColumn exists to induce; the real HeuristicRung refuses to make it."""

    column: int
    rung_id: int = 4
    family: Family = Family.STRUCTURAL

    def resolve(self, page: PageSource, target: FieldTarget) -> ResolvedElement | None:
        hits = page.query(f"main table tbody tr:first-child > td:nth-of-type({self.column})")
        if len(hits) != 1:
            return None
        return ResolvedElement(
            selector=f"td:nth-of-type({self.column})",
            node_id=page.node_id(hits[0]),
            anchor=AnchorEvidence(header_text=None),
            rung_id=self.rung_id,
        )


def _money(text: str) -> Decimal:
    return Decimal(text.strip().replace(",", ""))


def _bench() -> BenchPage:
    return BenchPage(render_results(FIXTURE_DAY), render_results(FIXTURE_DAY, mark_truth=True))


def _text(page: BenchPage, path: str) -> str:
    tree = lh.fromstring(page.html).getroottree()
    found = tree.xpath(path)
    assert len(found) == 1, path
    return str(found[0].text_content())


def _rows(page: BenchPage, amount_column: int | None = None) -> list[dict[str, str]]:
    """The page's records, read at the answer key's cells. With `amount_column`, amount is
    read from that td index of each record instead -- what a positional heal would extract."""
    paths = {t.name: page.truth(t.name) for t in FIELDS}
    n = len(paths["claim_number"])
    assert n > 0 and all(len(p) == n for p in paths.values())
    rows = [{name: _text(page, p[i]) for name, p in paths.items()} for i in range(n)]
    if amount_column is not None:
        for i, row in enumerate(rows, start=1):
            row["amount"] = _text(page, f"{ROWS}[{i}]/td[{amount_column}]")
    return rows


def _history() -> History:
    """Yesterday's accepted run: the unmutated bench page, read at the true cells."""
    return History(
        runs=(
            AcceptedRun(
                run_key="testbench:2:2026-09-08",
                window=FIXTURE_DAY,
                rows=tuple(_rows(_bench())),
                natural_key=("claim_number",),
            ),
        )
    )


def _amount_heal(page: BenchPage, winner: int) -> HealContext:
    return ctx(AMOUNT, healed=True, rung=winner, page=page.source())


def _gate(
    run: CandidateExtraction, amount_ctx: HealContext, rungs: list[Rung]
) -> ValidatedExtraction | QuarantinedRun:
    steady = {t.name: ctx(t, healed=False, rung=1, page=amount_ctx.page) for t in FIELDS}
    contexts = steady | {"amount": amount_ctx}
    checks: dict[str, list[Assertion]] = {t.name: [RowKeyContinuity()] for t in FIELDS}
    checks["amount"] = [IndependentRungAgreement(rungs), RowKeyContinuity()]
    return gate_run(run, _history(), contexts, checks)


REAL = [AxTreeRung(), AnchorRung(), HeuristicRung()]


# ---- adversarial: refused ------------------------------------------------------------------


@pytest.mark.unit
def test_real_text_rungs_refuse_a_positional_heal_onto_the_bench_decoy_column() -> None:
    page = _bench().mutated("DecoyColumn")
    stale = StaleColumnRung(AMOUNT_COLUMN_BEFORE)
    landed = stale.resolve(page.source(), AMOUNT)
    assert landed is not None and landed.node_id not in page.truth("amount")
    # It IS the decoy column: its header is a decoy label, and every value sits below the
    # same record's real amount (DecoyColumn draws 80-97% of it).
    header = _text(page, f"/html/body/main/table/thead/tr/th[{AMOUNT_COLUMN_BEFORE}]").strip()
    assert header in DECOY_LABELS
    for decoy, real in zip(_rows(page, AMOUNT_COLUMN_BEFORE), _rows(page), strict=True):
        assert _money(decoy["amount"]) < _money(real["amount"])

    v = IndependentRungAgreement([AxTreeRung(), AnchorRung(), stale]).check(
        candidate(_rows(page, AMOUNT_COLUMN_BEFORE)), _history(), _amount_heal(page, 4)
    )
    assert v.outcome is Outcome.FAIL and v.confirmed_by is None
    assert "cross-family disagreement" in v.reason
    assert v.observed is not None and page.truth("amount")[0] in v.observed


@pytest.mark.unit
def test_row_key_continuity_refuses_the_bench_decoy_values() -> None:
    page = _bench().mutated("DecoyColumn")
    run = candidate(_rows(page, AMOUNT_COLUMN_BEFORE))
    v = RowKeyContinuity().check(run, _history(), _amount_heal(page, 4))
    assert v.outcome is Outcome.FAIL and v.observed is not None and float(v.observed) < 0.5


@pytest.mark.unit
def test_the_bench_decoy_heal_is_quarantined_by_the_gate() -> None:
    page = _bench().mutated("DecoyColumn")
    stale = StaleColumnRung(AMOUNT_COLUMN_BEFORE)
    out = _gate(
        candidate(_rows(page, AMOUNT_COLUMN_BEFORE)),
        _amount_heal(page, 4),
        [AxTreeRung(), AnchorRung(), stale],
    )
    assert isinstance(out, QuarantinedRun)
    failed = {v.check for v in out.verdicts if v.field == "amount" and v.outcome is Outcome.FAIL}
    assert failed == {"IndependentRungAgreement", "RowKeyContinuity"}


@pytest.mark.unit
def test_bench_value_swap_passes_every_dom_signal_and_is_refused_by_values() -> None:
    page = _bench().mutated("ValueSwap")
    run = candidate(_rows(page))  # read at the true Amount node, which now holds other values
    dom = IndependentRungAgreement(REAL).check(run, _history(), _amount_heal(page, 2))
    assert dom.outcome is Outcome.PASS and dom.confirmed_by == Confirmation(
        rung=4, family=Family.STRUCTURAL
    )
    assert RowKeyContinuity().check(run, _history(), _amount_heal(page, 2)).outcome is Outcome.FAIL


# ---- benign: accepted ----------------------------------------------------------------------


@pytest.mark.unit
def test_a_correct_heal_on_the_decoy_page_is_confirmed_across_families() -> None:
    """The adversarial page does not make the gate refuse a CORRECT heal: real rung 4 takes
    the true Amount, and real rung 2 confirms the same node."""
    page = _bench().mutated("DecoyColumn")
    v = IndependentRungAgreement(REAL).check(
        candidate(_rows(page)), _history(), _amount_heal(page, 4)
    )
    assert v.outcome is Outcome.PASS
    assert v.confirmed_by == Confirmation(rung=2, family=Family.TEXT_SEMANTIC)


@pytest.mark.unit
def test_bench_label_reword_heals_by_rung_four_and_is_confirmed_by_values() -> None:
    """R14/C3 on the real bench: rungs 2 and 3 find nothing after the reword, rung 4 heals,
    and the VALUE family confirms -- so the run is written, not quarantined."""
    page = _bench().mutated("LabelReword")
    assert AxTreeRung().resolve(page.source(), AMOUNT) is None
    assert AnchorRung().resolve(page.source(), AMOUNT) is None
    run = candidate(_rows(page))
    v = IndependentRungAgreement(REAL).check(run, _history(), _amount_heal(page, 4))
    assert v.outcome is Outcome.PASS and v.observed == "row-key continuity"
    assert v.confirmed_by == Confirmation(rung=None, family=Family.VALUE)
    assert isinstance(_gate(run, _amount_heal(page, 4), REAL), ValidatedExtraction)


@pytest.mark.unit
def test_bench_class_rename_heals_by_rung_two_and_is_confirmed_by_rung_four() -> None:
    page = _bench().mutated("ClassRename")
    run = candidate(_rows(page))
    v = IndependentRungAgreement(REAL).check(run, _history(), _amount_heal(page, 2))
    assert v.outcome is Outcome.PASS
    assert v.confirmed_by == Confirmation(rung=4, family=Family.STRUCTURAL)
    assert isinstance(_gate(run, _amount_heal(page, 2), REAL), ValidatedExtraction)
