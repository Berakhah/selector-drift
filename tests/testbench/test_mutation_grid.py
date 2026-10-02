from __future__ import annotations

import pytest

from testbench.mutations.base import parse, serialise
from testbench.mutations.grid import (
    clone,
    data_rows,
    grids,
    header_cells,
    header_row,
    permute,
    set_text,
    text_of,
)
from testbench.portal.render import PAGE_SIZE, render_results
from tests.testbench.helpers import FIXTURE_DAY

GRID = (
    '<!DOCTYPE html><html><body><div role="grid">'
    '<div role="row"><span role="columnheader"><img alt="Amount"></span>'
    '<span role="columnheader" aria-label="Reserve">R</span></div>'
    '<div role="row"><span role="gridcell" id="c1" data-sd-truth="amount">'
    '<b><i>1,200.00</i></b></span> <span role="gridcell">90.50</span></div>'
    "</div></body></html>"
)


@pytest.mark.unit
def test_the_grid_is_found_as_a_table_or_as_role_annotated_divs() -> None:
    for html in (render_results(FIXTURE_DAY), GRID):
        doc = parse(html)
        (grid,) = grids(doc)
        head = header_row(grid)
        assert head is not None and len(header_cells(head)) in (2, 6)
    assert len(data_rows(grids(parse(render_results(FIXTURE_DAY)))[0])) == PAGE_SIZE


@pytest.mark.unit
def test_text_of_reads_accessible_text() -> None:
    doc = parse(GRID)
    assert [text_of(h) for h in doc.xpath('//*[@role="columnheader"]')] == ["Amount", "Reserve"]


@pytest.mark.unit
def test_set_text_replaces_the_innermost_text_and_keeps_the_wrappers() -> None:
    doc = parse(GRID)
    cell = doc.xpath('//*[@id="c1"]')[0]
    set_text(cell, "7.00")
    assert serialise(doc).count("<b><i>7.00</i></b>") == 1
    assert cell.get("data-sd-truth") == "amount"


@pytest.mark.unit
def test_a_clone_never_carries_a_bench_attribute_or_a_duplicate_id() -> None:
    doc = parse(GRID)
    cell = doc.xpath('//*[@id="c1"]')[0]
    twin = clone(cell, id_suffix="-copy")
    assert twin.get("id") == "c1-copy" and twin.get("data-sd-truth") is None
    assert twin.tail is None and twin.getparent() is None
    assert cell.get("data-sd-truth") == "amount"  # the original is untouched


@pytest.mark.unit
def test_permute_moves_cells_and_leaves_whitespace_in_place() -> None:
    doc = parse(GRID)
    row = doc.xpath('//*[@role="row"]')[1]
    cells = row.xpath('./*[@role="gridcell"]')
    permute(cells, (1, 0))
    assert [text_of(c) for c in row.xpath('./*[@role="gridcell"]')] == ["90.50", "1,200.00"]
    assert row[0].tail == " "
