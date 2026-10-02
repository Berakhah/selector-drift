from __future__ import annotations

import random

import pytest

from testbench.mutations.adversarial.decoy_column import DecoyColumn
from testbench.mutations.base import MutationBase
from testbench.mutations.column_reorder import ColumnReorder
from testbench.mutations.dom_restructure import DomRestructure
from testbench.mutations.label_reword import LabelReword
from testbench.mutations.table_to_div import TableToDiv
from testbench.portal.data import COLUMNS
from testbench.portal.render import PAGE_SIZE, render_results
from tests.testbench.helpers import FIXTURE_DAY, column, headers, is_plausible_amount, root, rows

LABELS = [label for _, label in COLUMNS]
BEFORE = render_results(FIXTURE_DAY)


@pytest.mark.unit
def test_decoy_is_indistinguishable_by_value_alone() -> None:
    after = DecoyColumn().apply(BEFORE)
    real, decoy = column(after, "Amount"), column(after, "Prior Amount")
    assert len(decoy) == PAGE_SIZE
    assert all(is_plausible_amount(v) for v in real + decoy)
    assert all(r != d for r, d in zip(real, decoy, strict=True))  # every row, not the list


@pytest.mark.unit
def test_decoy_sits_adjacent_to_the_real_column() -> None:
    hs = headers(DecoyColumn().apply(BEFORE))
    assert hs.index("Prior Amount") == hs.index("Amount") - 1


@pytest.mark.unit
def test_an_index_keyed_resolver_now_reads_the_decoy() -> None:
    """The cached td:nth-child(3) held Amount on the clean page. It now holds the decoy."""
    third = LABELS.index("Amount")
    after = DecoyColumn().apply(BEFORE)
    assert [r[third] for r in rows(BEFORE)] == column(BEFORE, "Amount")
    assert [r[third] for r in rows(after)] == column(after, "Prior Amount")


@pytest.mark.unit
def test_decoy_leaves_every_real_column_intact() -> None:
    after = DecoyColumn().apply(BEFORE)
    for label in LABELS:
        assert column(after, label) == column(BEFORE, label), label


@pytest.mark.unit
def test_the_decoy_is_styled_exactly_like_amount() -> None:
    """Same classes as the real cell: a class selector now matches two cells per row, so the
    clean run's cached selector cannot hold at rung 1 and a heal is forced."""
    first_row = root(DecoyColumn().apply(BEFORE)).xpath("//tbody/tr[1]")[0]
    assert len(first_row.xpath('./td[@class="col-amount"]')) == 2
    assert len(first_row.xpath("./td")) == len(LABELS) + 1


@pytest.mark.unit
def test_the_decoy_never_carries_the_truth_marker() -> None:
    """Task 8's key: a clone keeps no data-sd-truth, or the answer would name two columns."""
    doc = root(DecoyColumn().apply(render_results(FIXTURE_DAY, mark_truth=True)))
    decoys = doc.xpath(f"//tbody/tr/td[{LABELS.index('Amount') + 1}]")
    assert len(decoys) == PAGE_SIZE
    assert all(d.get("data-sd-truth") is None for d in decoys)
    marked = doc.xpath('//*[@data-sd-truth="amount"]')
    assert [el.text_content() for el in marked] == column(BEFORE, "Amount")


@pytest.mark.unit
@pytest.mark.parametrize(
    "first", [ColumnReorder(), DomRestructure(), TableToDiv(), LabelReword()], ids=lambda m: m.name
)
def test_decoy_finds_amount_after_any_benign_change(first: MutationBase) -> None:
    """Adversarial classes run last, on top of the day's benign ones (Task 8)."""
    after = DecoyColumn().apply(first.apply(BEFORE))
    hs = headers(after)
    amount = next(h for h in hs if h in ("Amount", "Amount (USD)"))
    assert hs.index("Prior Amount") == hs.index(amount) - 1
    assert column(after, amount) == column(BEFORE, "Amount")
    assert all(is_plausible_amount(v) for v in column(after, "Prior Amount"))


@pytest.mark.unit
def test_decoy_values_and_label_vary_with_the_rng() -> None:
    drawn = [DecoyColumn.from_rng(random.Random(seed)) for seed in range(12)]
    pages = {m.apply(BEFORE) for m in drawn}
    assert len(pages) == len(drawn)
    assert {m.label for m in drawn} == {"Prior Amount", "Previous Amount", "Original Amount"}
