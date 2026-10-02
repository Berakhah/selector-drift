from __future__ import annotations

import random
from decimal import Decimal

import pytest

from testbench.mutations.adversarial.decoy_column import DecoyColumn
from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.column_reorder import ColumnReorder
from testbench.mutations.dom_restructure import DomRestructure
from testbench.mutations.label_reword import LabelReword
from testbench.mutations.table_to_div import TableToDiv
from testbench.portal.data import COLUMNS, dataset_for
from testbench.portal.render import PAGE_SIZE, render_detail, render_login, render_results
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


# --- Fix round 1: the ratio band must hold even near MIN_MONEY (controller's ruling) ---------


@pytest.mark.unit
@pytest.mark.parametrize(
    "real", ["103.10", "104.00", "125.00", "1000.00", "249999.99", "250000.00"]
)
def test_decoy_value_stays_in_the_ratio_band(real: str) -> None:
    mutation = DecoyColumn()
    for row in range(50):
        decoy = mutation.decoy_value(real, row)
        assert is_plausible_amount(decoy)
        ratio = Decimal(decoy.replace(",", "")) / Decimal(real.replace(",", ""))
        assert Decimal("0.80") <= ratio <= Decimal("0.97"), (real, row, decoy, ratio)


@pytest.mark.unit
@pytest.mark.parametrize("real", ["100.01", "101.00", "103.00"])
def test_decoy_value_below_the_feasible_floor_clamps_to_min_money(real: str) -> None:
    """Below MIN_MONEY / high (~103.09) the 80-97% band would fall under MIN_MONEY -- the
    nearest plausible value strictly below `real` is MIN_MONEY itself."""
    mutation = DecoyColumn()
    for row in range(5):
        decoy = mutation.decoy_value(real, row)
        assert decoy == "100.00"
        assert Decimal(decoy) < Decimal(real)


@pytest.mark.unit
def test_decoy_value_at_the_floor_is_one_cent_above() -> None:
    """At real == MIN_MONEY exactly there is no plausible value strictly below it; plausibility
    outranks the band, so this is the single documented out-of-band case."""
    mutation = DecoyColumn()
    decoy = mutation.decoy_value("100.00", 0)
    assert decoy == "100.01"
    assert decoy != "100.00"
    assert is_plausible_amount(decoy)


@pytest.mark.unit
def test_decoy_column_is_a_no_op_on_pages_without_a_results_grid() -> None:
    login_html = render_login()
    assert DecoyColumn().apply(login_html) == serialise(parse(login_html))
    detail_html = render_detail(FIXTURE_DAY, dataset_for(FIXTURE_DAY)[0].claim_number)
    assert detail_html is not None
    assert DecoyColumn().apply(detail_html) == serialise(parse(detail_html))
