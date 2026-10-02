"""Task 11A: each adversarial class really puts its attack in the served markup.

These tests prove the MARKUP property only. Whether the gate refuses each class -- and by which
verdict -- is Task 26's adversarial suite, which runs once gate() and QuarantinedRun exist.
"""

from __future__ import annotations

import random

import lxml.html
import pytest

from testbench.mutations.adversarial.duplicate_label import DuplicateLabel
from testbench.mutations.adversarial.hidden_decoy import VARIANTS, HiddenDecoy
from testbench.mutations.adversarial.image_header import ImageHeader
from testbench.mutations.adversarial.locale_flip import LocaleFlip, flip
from testbench.mutations.adversarial.value_swap import ValueSwap
from testbench.mutations.adversarial.virtualised_rows import SCRIPT, VirtualisedRows
from testbench.mutations.base import CatalogueEntry, MutationBase, parse, serialise
from testbench.mutations.catalogue import CATALOGUE
from testbench.mutations.column_reorder import ColumnReorder
from testbench.mutations.dom_restructure import DomRestructure
from testbench.mutations.label_reword import LabelReword
from testbench.mutations.table_to_div import TableToDiv
from testbench.portal.data import COLUMNS, dataset_for
from testbench.portal.render import PAGE_SIZE, render_detail, render_login, render_results
from tests.testbench.helpers import (
    CLAIM_NUMBER,
    FIXTURE_DAY,
    cells,
    column,
    headers,
    is_plausible_amount,
    root,
    rows,
    skeleton,
)

LABELS = [label for _, label in COLUMNS]
BEFORE = render_results(FIXTURE_DAY)
REAL_NUMBERS = column(BEFORE, "Claim Number")


def _is_hidden(el: lxml.html.HtmlElement) -> bool:
    for node in (el, *el.iterancestors()):
        style = (node.get("style") or "").replace(" ", "")
        if node.get("aria-hidden") == "true" or "display:none" in style:
            return True
    return False


def _table_column(html: str, position: int) -> list[str]:
    """Read the real <table> by position, ignoring any decoy grid a mutation added."""
    return [td.text_content() for td in root(html).xpath(f"//table//tbody/tr/td[{position}]")]


# --- HiddenDecoy: rung 2's "right name, wrong element" (H9) --------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("variant", VARIANTS)
def test_hidden_decoy_adds_hidden_well_formed_claim_numbers_ahead_of_the_real_ones(
    variant: str,
) -> None:
    after = HiddenDecoy(variant=variant).apply(BEFORE)
    doc = root(after)
    decoys = doc.xpath('//*[@aria-label="Claim Number"]')
    assert len(decoys) == PAGE_SIZE
    assert all(_is_hidden(d) for d in decoys)
    values = [d.text for d in decoys]
    assert all(CLAIM_NUMBER.fullmatch(v) for v in values)
    assert not set(values) & set(REAL_NUMBERS)
    order = list(doc.iter())
    assert order.index(decoys[0]) < order.index(doc.xpath("//table")[0])  # first in the DOM
    assert _table_column(after, 1) == REAL_NUMBERS


@pytest.mark.unit
def test_hidden_decoy_draws_its_variant_and_numbers_from_the_rng() -> None:
    drawn = [HiddenDecoy.from_rng(random.Random(seed)) for seed in range(12)]
    assert {m.variant for m in drawn} == set(VARIANTS)
    assert len({m.offset for m in drawn}) == len(drawn)


@pytest.mark.unit
def test_hidden_decoy_still_targets_a_reworded_header() -> None:
    """After LabelReword the visible header says "Claim No."; the decoy keeps the clean run's
    anchor text, which is exactly what a cached anchor would look for."""
    doc = root(HiddenDecoy().apply(LabelReword().apply(BEFORE)))
    assert len(doc.xpath('//*[@aria-label="Claim Number"]')) == PAGE_SIZE


# --- DuplicateLabel: rung 1's "exactly one" and rung 3's anchor ----------------------------


@pytest.mark.unit
def test_duplicate_label_yields_exactly_two_claim_number_headers() -> None:
    after = DuplicateLabel().apply(BEFORE)
    assert headers(after).count("Claim Number") == 2
    assert headers(after)[:2] == ["Claim Number", "Claim Number"]
    legacy, real = [r[0] for r in rows(after)], [r[1] for r in rows(after)]
    assert real == REAL_NUMBERS
    assert all(CLAIM_NUMBER.fullmatch(v) for v in legacy)
    assert all(a != b for a, b in zip(legacy, real, strict=True))


@pytest.mark.unit
def test_duplicate_label_makes_the_old_class_selector_ambiguous() -> None:
    doc = root(DuplicateLabel().apply(BEFORE))
    first_row = doc.xpath("//tbody/tr[1]")[0]
    assert len(first_row.xpath('./td[@class="col-claim_number"]')) == 2


@pytest.mark.unit
def test_duplicate_label_copies_the_label_it_finds() -> None:
    after = DuplicateLabel().apply(LabelReword().apply(BEFORE))
    assert headers(after).count("Claim No.") == 2 and "Claim Number" not in headers(after)


@pytest.mark.unit
def test_the_legacy_column_never_carries_the_truth_marker() -> None:
    doc = root(DuplicateLabel().apply(render_results(FIXTURE_DAY, mark_truth=True)))
    marked = doc.xpath('//*[@data-sd-truth="claim_number"]')
    assert len(marked) == PAGE_SIZE
    assert [el.text_content() for el in marked] == REAL_NUMBERS


# --- ValueSwap: invisible to every structural and textual check -----------------------------


@pytest.mark.unit
def test_value_swap_is_invisible_to_cross_field_checks() -> None:
    """Bench spec §6.3. Every swapped value is still a plausible amount in a plausible column,
    the markup skeleton is identical, and Task 7 proved no footing rule relates the two
    columns -- so nothing but RowKeyContinuity (Task 22) can see it. Task 26 proves the gate
    half."""
    after = ValueSwap().apply(BEFORE)
    assert column(after, "Amount") == column(BEFORE, "Reserve")
    assert column(after, "Reserve") == column(BEFORE, "Amount")
    assert all(is_plausible_amount(v) for v in column(after, "Amount") + column(after, "Reserve"))
    assert skeleton(after) == skeleton(BEFORE)


@pytest.mark.unit
def test_value_swap_touches_no_other_column_and_is_its_own_inverse() -> None:
    after = ValueSwap().apply(BEFORE)
    for label in ("Claim Number", "Claimant", "Status", "Filed"):
        assert column(after, label) == column(BEFORE, label)
    assert ValueSwap().apply(after) == serialise(parse(BEFORE))


@pytest.mark.unit
def test_value_swap_follows_the_columns_through_a_reorder() -> None:
    reordered = ColumnReorder().apply(BEFORE)
    after = ValueSwap().apply(reordered)
    assert column(after, "Amount") == column(BEFORE, "Reserve")
    assert skeleton(after) == skeleton(reordered)


# --- LocaleFlip: 1,234.56 -> 1.234,56 ------------------------------------------------------


@pytest.mark.unit
def test_locale_flip_rewrites_every_currency_value() -> None:
    after = LocaleFlip().apply(BEFORE)
    for label in ("Amount", "Reserve"):
        assert column(after, label) == [flip(v) for v in column(BEFORE, label)]
        assert not any(is_plausible_amount(v) for v in column(after, label))
    for label in ("Claim Number", "Claimant", "Status", "Filed"):
        assert column(after, label) == column(BEFORE, label)
    assert flip("198,178.68") == "198.178,68" and flip("193.45") == "193,45"


@pytest.mark.unit
def test_locale_flip_rewrites_the_detail_page_and_is_idempotent() -> None:
    claim = dataset_for(FIXTURE_DAY)[0]
    detail = render_detail(FIXTURE_DAY, claim.claim_number)
    assert detail is not None
    after = LocaleFlip().apply(detail)
    values = [dd.text_content() for dd in root(after).xpath("//dl/dd")]
    assert values[LABELS.index("Amount")] == flip(f"{claim.amount:,.2f}")
    assert values[LABELS.index("Reserve")] == flip(f"{claim.reserve:,.2f}")
    assert LocaleFlip().apply(after) == after


# --- VirtualisedRows: the only script, all rows still served --------------------------------


@pytest.mark.unit
def test_virtualised_rows_adds_one_inline_script_and_keeps_every_served_row() -> None:
    after = VirtualisedRows().apply(BEFORE)
    doc = root(after)
    scripts = doc.xpath("//script")
    assert len(scripts) == 1
    body = doc.xpath("//body")[0]
    assert body[-1] is scripts[0] and scripts[0].text == SCRIPT
    assert scripts[0].get("src") is None
    assert f"<script>{SCRIPT}</script>" in after  # serialised verbatim
    assert len(rows(after)) == PAGE_SIZE and cells(after) == cells(BEFORE)


@pytest.mark.unit
def test_virtualised_rows_is_idempotent_and_leaves_pages_without_rows_alone() -> None:
    once = VirtualisedRows().apply(BEFORE)
    assert VirtualisedRows().apply(once) == once
    assert "<script" not in VirtualisedRows().apply(render_login())


@pytest.mark.unit
@pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.name)
def test_only_virtualised_rows_emits_a_script(entry: CatalogueEntry) -> None:
    out = entry.build(random.Random(0)).apply(BEFORE)
    assert ("<script" in out) is (entry.name == "VirtualisedRows")


# --- ImageHeader: the header is an image of its label ---------------------------------------


@pytest.mark.unit
def test_image_header_replaces_the_amount_header_text_with_an_image() -> None:
    after = ImageHeader().apply(BEFORE)
    doc = root(after)
    th = doc.xpath("//th")[LABELS.index("Amount")]
    assert th.text_content() == ""
    assert [(img.get("alt"), img.get("src")) for img in th.xpath("./img")] == [("Amount", None)]
    assert headers(after) == headers(BEFORE)  # accessible text is unchanged
    assert rows(after) == rows(BEFORE)
    others = [h.text_content() for h in doc.xpath("//th")]
    assert others == [label if label != "Amount" else "" for label in LABELS]


@pytest.mark.unit
@pytest.mark.parametrize(
    "first", [DomRestructure(), TableToDiv(), LabelReword()], ids=lambda m: m.name
)
def test_image_header_follows_the_header_through_benign_changes(first: MutationBase) -> None:
    before = first.apply(BEFORE)
    after = ImageHeader().apply(before)
    assert headers(after) == headers(before)
    alts = [img.get("alt") for img in root(after).xpath("//img")]
    assert alts == [h for h in headers(before) if h in ("Amount", "Amount (USD)")]
    assert ImageHeader().apply(after) == after
