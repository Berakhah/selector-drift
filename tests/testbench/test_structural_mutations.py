from __future__ import annotations

import random

import pytest

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.class_rename import ClassRename
from testbench.mutations.column_reorder import ColumnReorder
from testbench.mutations.dom_restructure import DomRestructure
from testbench.mutations.table_to_div import TableToDiv
from testbench.portal.data import COLUMNS
from testbench.portal.render import PAGE_SIZE, render_results
from tests.testbench.helpers import FIXTURE_DAY, cells, column, headers, root, rows

LABELS = [label for _, label in COLUMNS]
BEFORE = render_results(FIXTURE_DAY)
STRUCTURAL: list[MutationBase] = [ClassRename(), DomRestructure(), ColumnReorder(), TableToDiv()]


def class_tokens(html: str) -> set[str]:
    return {
        t
        for el in root(html).iter()
        if isinstance(el.tag, str)
        for t in (el.get("class") or "").split()
    }


def first_cell_path(html: str, label: str) -> str:
    doc = root(html)
    tree = doc.getroottree()
    header = headers(html).index(label)
    first = doc.xpath(
        '(//tr | //*[@role="row"])[td or *[@role="cell"]][1]/*[self::td or @role="cell"]'
    )[header]
    return str(tree.getpath(first))


@pytest.mark.unit
@pytest.mark.parametrize("mutation", STRUCTURAL, ids=lambda m: m.name)
def test_mutation_changes_markup_but_preserves_values(mutation: MutationBase) -> None:
    after = mutation.apply(BEFORE)
    assert after != serialise(parse(BEFORE))
    assert sorted(cells(after)) == sorted(cells(BEFORE))
    assert sorted(headers(after)) == sorted(headers(BEFORE))
    for label in LABELS:  # every value still sits under its own header
        assert column(after, label) == column(BEFORE, label), label


@pytest.mark.unit
def test_column_reorder_actually_moves_positions() -> None:
    after = ColumnReorder().apply(BEFORE)
    assert headers(after) == [LABELS[i] for i in ColumnReorder().order]
    assert cells(after) != cells(BEFORE)
    assert first_cell_path(after, "Amount") != first_cell_path(BEFORE, "Amount")


@pytest.mark.unit
def test_column_reorder_draws_a_real_permutation_from_the_rng() -> None:
    drawn = [ColumnReorder.from_rng(random.Random(seed)).order for seed in range(50)]
    for order in drawn:
        assert sorted(order) == list(range(len(LABELS))) and list(order) != sorted(order)
    assert len(set(drawn)) >= 10
    with pytest.raises(ValueError, match="identity"):
        ColumnReorder(order=(0, 1, 2, 3, 4, 5))
    with pytest.raises(ValueError, match="permutation"):
        ColumnReorder(order=(0, 0, 1, 2, 3, 4))


@pytest.mark.unit
def test_column_reorder_leaves_a_grid_of_another_width_alone() -> None:
    narrow = (
        "<!DOCTYPE html><html><body><table><tr><th>A</th><th>B</th></tr>"
        "<tr><td>1</td><td>2</td></tr></table></body></html>"
    )
    assert ColumnReorder().apply(narrow) == serialise(parse(narrow))


@pytest.mark.unit
@pytest.mark.parametrize("scheme", ["hashed", "prefixed", "bem"])
def test_class_rename_renames_every_class_consistently(scheme: str) -> None:
    mutation = ClassRename(scheme=scheme, salt="q7w2e")
    after = mutation.apply(BEFORE)
    old, new = class_tokens(BEFORE), class_tokens(after)
    assert old and not (old & new)
    assert new == {mutation.rename(t) for t in old}  # one new name per old name
    amount = root(after).xpath("//tbody/tr/td[3]")
    assert {c.get("class") for c in amount} == {mutation.rename("col-amount")}


@pytest.mark.unit
def test_class_rename_varies_with_the_rng() -> None:
    drawn = {ClassRename.from_rng(random.Random(seed)) for seed in range(20)}
    assert len(drawn) == 20 and len({m.scheme for m in drawn}) == 3


@pytest.mark.unit
def test_dom_restructure_moves_every_cell_and_promotes_the_caption() -> None:
    mutation = DomRestructure(wrappers=("card", "card-body", "table-responsive"), inner_tag="div")
    after = mutation.apply(BEFORE)
    doc = root(after)
    assert not doc.xpath("//caption")
    assert [h.text_content() for h in doc.xpath('//h2[@class="grid-title"]')] == [
        "Claims matching your search"
    ]
    table = doc.xpath("//table")[0]
    assert [a.get("class") for a in table.iterancestors("div")][:3] == [
        "table-responsive",
        "card-body",
        "card",
    ]
    assert len(doc.xpath('//td/div[@class="cell-content"]')) == PAGE_SIZE * len(LABELS)
    assert first_cell_path(after, "Claim Number") != first_cell_path(BEFORE, "Claim Number")


@pytest.mark.unit
def test_dom_restructure_keeps_the_claim_link() -> None:
    after = DomRestructure().apply(BEFORE)
    links = root(after).xpath('//td/span[@class="cell-content"]/a[starts-with(@href, "/claims/")]')
    assert [a.text for a in links] == column(BEFORE, "Claim Number")


@pytest.mark.unit
def test_table_to_div_leaves_a_role_annotated_grid_and_no_table() -> None:
    doc = root(TableToDiv().apply(BEFORE))
    for tag in ("table", "caption", "thead", "tbody", "tr", "th", "td"):
        assert not doc.xpath(f"//{tag}"), tag
    count = {
        role: len(doc.xpath(f'//div[@role="{role}"]'))
        for role in ("table", "rowgroup", "row", "columnheader", "cell")
    }
    assert count == {
        "table": 1,
        "rowgroup": 2,
        "row": PAGE_SIZE + 1,
        "columnheader": len(LABELS),
        "cell": PAGE_SIZE * len(LABELS),
    }
    assert doc.xpath('//div[@role="table"]')[0].get("id") == "results"  # attributes kept


@pytest.mark.unit
def test_table_to_div_is_idempotent() -> None:
    once = TableToDiv().apply(BEFORE)
    assert TableToDiv().apply(once) == once


@pytest.mark.unit
def test_structural_classes_compose_in_either_order() -> None:
    """A day applies up to two benign classes in a random order (Task 8), so each must read
    the other's output. Values still sit under their own headers either way round."""
    for first, second in [
        (TableToDiv(), ColumnReorder()),
        (ColumnReorder(), TableToDiv()),
        (TableToDiv(), DomRestructure()),
        (DomRestructure(), TableToDiv()),
        (DomRestructure(), ColumnReorder()),
    ]:
        after = second.apply(first.apply(BEFORE))
        assert len(rows(after)) == PAGE_SIZE, (first.name, second.name)
        for label in LABELS:
            assert column(after, label) == column(BEFORE, label), (first.name, second.name)
