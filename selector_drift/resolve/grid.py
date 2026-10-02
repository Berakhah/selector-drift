"""Tables and grids by role, so a native <table> and TableToDiv's role="table" divs are one model.

Column positions count colspan (aria-colspan on divs). A multi-level header's grouping row
(the one with "Money" spanning "Amount"/"Reserve") routinely has a plain, unlabelled corner
cell that is not itself a columnheader -- a membership test that required EVERY cell in a row
to be a columnheader (`is_header_row`) let that row slip through as "data" and the corner cell
got returned as a value (fix round 2, I1). `header_band()` is the real boundary: every row
from the top, up to and including the last row that still looks like header -- it carries a
columnheader cell, or any cell whose colspan/rowspan (or the aria-* equivalent) is greater than
one -- stopping at the first row with neither. `data_rows()`/`first_data_row()` start after the
band, so no rung can be handed a band cell as a data value; and a cell with `rowspan`/
`aria-rowspan` > 1 anywhere in the band (`header_has_rowspan()`) means colspan-only column
position cannot be trusted to line up at all, so `cell_under()` declines outright rather than
guess. `in_header_band()` is the belt-and-suspenders check for a path -- `cell_right_of()`,
walking a single row -- that does not go through `data_rows()` and so would not otherwise be
stopped from handing back a band cell.
"""

from __future__ import annotations

from lxml.html import HtmlElement

from selector_drift.resolve.aria import CELL_ROLES, DATA_CELL_ROLES, TABLE_ROLES, ancestors
from selector_drift.resolve.labels import normalise
from selector_drift.resolve.page import PageSource


def nearest(
    page: PageSource, el: HtmlElement, roles: frozenset[str], *, include_self: bool = True
) -> HtmlElement | None:
    for node in ([el] if include_self else []) + ancestors(el):
        if page.role(node) in roles:
            return node
    return None


def grid_of(page: PageSource, el: HtmlElement) -> HtmlElement | None:
    return nearest(page, el, TABLE_ROLES)


def row_of(page: PageSource, el: HtmlElement) -> HtmlElement | None:
    return nearest(page, el, frozenset({"row"}))


def _descendants(el: HtmlElement) -> list[HtmlElement]:
    return [
        d for d in el.iterdescendants() if isinstance(d, HtmlElement) and isinstance(d.tag, str)
    ]


def rows(page: PageSource, grid: HtmlElement) -> list[HtmlElement]:
    return [
        d
        for d in _descendants(grid)
        if page.role(d) == "row" and nearest(page, d, TABLE_ROLES, include_self=False) is grid
    ]


def cells(page: PageSource, row: HtmlElement) -> list[HtmlElement]:
    return [d for d in _descendants(row) if page.role(d) in CELL_ROLES and row_of(page, d) is row]


def span(cell: HtmlElement) -> int:
    raw = cell.get("colspan") or cell.get("aria-colspan") or "1"
    return min(max(int(raw), 1), 1000) if raw.isdigit() else 1


def column_of(page: PageSource, cell: HtmlElement) -> int | None:
    row = row_of(page, cell)
    if row is None:
        return None
    index = 0
    for c in cells(page, row):
        if c is cell:
            return index
        index += span(c)
    return None


def cell_at(page: PageSource, row: HtmlElement, index: int) -> HtmlElement | None:
    start = 0
    for c in cells(page, row):
        if start <= index < start + span(c):
            return c
        start += span(c)
    return None


def width(page: PageSource, row: HtmlElement) -> int:
    return sum(span(c) for c in cells(page, row))


def is_header_row(page: PageSource, row: HtmlElement) -> bool:
    cs = cells(page, row)
    return bool(cs) and all(page.role(c) == "columnheader" for c in cs)


def header_rows(page: PageSource, grid: HtmlElement) -> list[HtmlElement]:
    return [r for r in rows(page, grid) if is_header_row(page, r)]


def _has_span(cell: HtmlElement) -> bool:
    colspan = cell.get("colspan") or cell.get("aria-colspan") or "1"
    rowspan = cell.get("rowspan") or cell.get("aria-rowspan") or "1"
    wide = colspan.isdigit() and int(colspan) > 1
    tall = rowspan.isdigit() and int(rowspan) > 1
    return wide or tall


def _looks_like_header(page: PageSource, row: HtmlElement) -> bool:
    cs = cells(page, row)
    return any(page.role(c) == "columnheader" for c in cs) or any(_has_span(c) for c in cs)


def header_band(page: PageSource, grid: HtmlElement) -> list[HtmlElement]:
    """Every row from the top, up to and including the last row that still looks like header
    (fix round 2, I1): it carries a columnheader cell, or any cell whose colspan/rowspan is
    greater than one. Stops at the first row with neither -- a row-header data row (a
    `<th scope=row>` beside plain `<td>`s, no spans) is correctly not part of the band."""
    band: list[HtmlElement] = []
    for r in rows(page, grid):
        if _looks_like_header(page, r):
            band.append(r)
        else:
            break
    return band


def in_header_band(page: PageSource, cell: HtmlElement) -> bool:
    grid, row = grid_of(page, cell), row_of(page, cell)
    if grid is None or row is None:
        return False
    return any(row is b for b in header_band(page, grid))


def header_has_rowspan(page: PageSource, grid: HtmlElement) -> bool:
    """True when any cell anywhere in the header band spans multiple rows (fix round 2, I1).
    `column_of` and `cell_at` count colspan only, so a rowspanned cell desynchronises column
    indices between rows of the band -- and between the band and the data row beneath it --
    and position alone can no longer be trusted to verify a header."""
    for r in header_band(page, grid):
        for c in cells(page, r):
            raw = c.get("rowspan") or c.get("aria-rowspan") or "1"
            if raw.isdigit() and int(raw) > 1:
                return True
    return False


def data_rows(page: PageSource, grid: HtmlElement) -> list[HtmlElement]:
    """Visible rows holding data. A single cell spanning a multi-column grid is a pager or a
    footer, not data (Pagination mutation). Rows in the header band (fix round 2, I1) never
    count as data, whatever role their own cells carry."""
    all_rows = rows(page, grid)
    band = header_band(page, grid)
    columns = max((width(page, r) for r in all_rows), default=0)
    out = []
    for r in all_rows:
        if any(r is b for b in band) or is_header_row(page, r) or not page.visible(r):
            continue
        cs = cells(page, r)
        if not cs:
            continue
        if not any(page.role(c) in DATA_CELL_ROLES for c in cs):
            continue
        if columns > 1 and len(cs) == 1:
            continue
        out.append(r)
    return out


def first_data_row(page: PageSource, grid: HtmlElement) -> HtmlElement | None:
    found = data_rows(page, grid)
    return found[0] if found else None


def column_header(page: PageSource, cell: HtmlElement) -> HtmlElement | None:
    """The header a cell sits under: its headers="" idrefs first, else by column position."""
    for ident in (cell.get("headers") or "").split():
        hits = page.root().xpath("//*[@id=$i]", i=ident)
        if isinstance(hits, list):
            for h in hits:
                if isinstance(h, HtmlElement) and page.role(h) == "columnheader":
                    return h
    grid, index = grid_of(page, cell), column_of(page, cell)
    if grid is None or index is None:
        return None
    for hr in reversed(header_rows(page, grid)):
        h = cell_at(page, hr, index)
        if h is not None and page.role(h) == "columnheader":
            return h
    return None


def row_header(page: PageSource, cell: HtmlElement) -> HtmlElement | None:
    row = row_of(page, cell)
    if row is None:
        return None
    for c in cells(page, row):
        if c is cell:
            return None
        if page.role(c) == "rowheader":
            return c
    return None


def cell_under(page: PageSource, header: HtmlElement) -> HtmlElement | None:
    """The first data row's cell in this header's column -- header-verified: the cell's own
    header, recomputed from the cell's position, must be this header. Declines outright
    (fix round 1, I1) when the grid has a rowspanned header cell anywhere: column position by
    colspan alone cannot be trusted to line up, and the verification below would only agree
    with the same miscounted position it computed the value from."""
    grid, index = grid_of(page, header), column_of(page, header)
    if grid is None or index is None or header_has_rowspan(page, grid):
        return None
    row = first_data_row(page, grid)
    if row is None:
        return None
    cell = cell_at(page, row, index)
    if cell is None or page.role(cell) not in DATA_CELL_ROLES:
        return None
    return cell if column_header(page, cell) is header else None


def cell_right_of(page: PageSource, header: HtmlElement) -> HtmlElement | None:
    """For a row header: the first data cell after it in its row."""
    row = row_of(page, header)
    if row is None:
        return None
    seen = False
    for c in cells(page, row):
        if c is header:
            seen = True
        elif seen and page.role(c) in DATA_CELL_ROLES:
            return c
    return None


def headers_named(page: PageSource, label: str) -> list[HtmlElement]:
    """Visible column headers anywhere on the page whose accessible name is `label`."""
    want = normalise(label)
    return [
        el
        for el in _descendants(page.root())
        if page.role(el) == "columnheader" and page.visible(el) and normalise(page.name(el)) == want
    ]


__all__ = [
    "cell_at",
    "cell_right_of",
    "cell_under",
    "cells",
    "column_header",
    "column_of",
    "data_rows",
    "first_data_row",
    "grid_of",
    "header_band",
    "header_has_rowspan",
    "header_rows",
    "headers_named",
    "in_header_band",
    "is_header_row",
    "nearest",
    "row_header",
    "row_of",
    "rows",
    "span",
    "width",
]
