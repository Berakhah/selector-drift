"""Tables and grids by role, so a native <table> and TableToDiv's role="table" divs are one model.

Column positions count colspan (aria-colspan on divs). A multi-level header's grouping row
(the one with "Money" spanning "Amount"/"Reserve") routinely has a plain, unlabelled corner
cell that is not itself a columnheader -- a membership test that required EVERY cell in a row
to be a columnheader (`is_header_row`) let that row slip through as "data" and the corner cell
got returned as a value (fix round 2, I1).

`header_band()` is the real boundary, and it is keyed on `columnheader` alone, never on spans
(fix round 3, I1): the band is every row from the top through the LAST row that contains a
`columnheader` cell, inclusive. A row above that line is in the band purely by position, even
if it carries no columnheader itself (the grouping row's plain corner cell) -- but a row
*below* that line is never swept in just because one of its cells has a colspan/rowspan: a
grouped line-item's rowspanned first cell, or a merged totals cell, is an ordinary and common
data-row shape, and keying the band on spans mistook both for header (fix round 2's bug: a
genuine second data row got pulled into the band by its own colspan, or a genuine data row's
own rowspan tripped `header_has_rowspan` and declined a perfectly good resolution). If the grid
has no `columnheader` anywhere, the band is empty -- a row-header table with no column headers
at all is not swept in merely because a value cell happens to span.

`data_rows()`/`first_data_row()` start after the band, so no rung can be handed a band cell as
a data value; and a cell with `rowspan`/`aria-rowspan` > 1 anywhere *in the band*
(`header_has_rowspan()`) means colspan-only column position cannot be trusted to line up at
all, so `cell_under()` declines outright rather than guess. `in_header_band()` is the
belt-and-suspenders check for a path -- `cell_right_of()`, walking a single row -- that does
not go through `data_rows()` and so would not otherwise be stopped from handing back a band
cell. All four take an optional pre-computed `band`, so a caller resolving several candidates
against the same grid (rung 2's cascade over the accessibility tree) computes it once.
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


def header_band(page: PageSource, grid: HtmlElement) -> list[HtmlElement]:
    """Every row from the top through the LAST row that contains a `columnheader` cell,
    inclusive (fix round 3, I1). A row above that line is in the band purely by position --
    P4's all-`<td>` grouping row, with no columnheader of its own, is swept in because it sits
    above the row that does have one. A row *below* that line is never swept in by a span on
    one of its own cells: a rowspanned first cell or a merged totals cell is an ordinary data
    row (round 2's bug). No `columnheader` anywhere on the grid -> an empty band."""
    all_rows = rows(page, grid)
    last = -1
    for i, r in enumerate(all_rows):
        if any(page.role(c) == "columnheader" for c in cells(page, r)):
            last = i
    return all_rows[: last + 1] if last >= 0 else []


def in_header_band(
    page: PageSource, cell: HtmlElement, band: list[HtmlElement] | None = None
) -> bool:
    grid, row = grid_of(page, cell), row_of(page, cell)
    if grid is None or row is None:
        return False
    if band is None:
        band = header_band(page, grid)
    return any(row is b for b in band)


def header_has_rowspan(
    page: PageSource, grid: HtmlElement, band: list[HtmlElement] | None = None
) -> bool:
    """True when any cell anywhere in the header band spans multiple rows. `column_of` and
    `cell_at` count colspan only, so a rowspanned cell desynchronises column indices between
    rows of the band -- and between the band and the data row beneath it -- and position alone
    can no longer be trusted to verify a header. A rowspan in a *data* row (grouped line items)
    is not this: the band excludes it (fix round 3, I1)."""
    for r in header_band(page, grid) if band is None else band:
        for c in cells(page, r):
            raw = c.get("rowspan") or c.get("aria-rowspan") or "1"
            if raw.isdigit() and int(raw) > 1:
                return True
    return False


def data_rows(
    page: PageSource, grid: HtmlElement, band: list[HtmlElement] | None = None
) -> list[HtmlElement]:
    """Visible rows holding data. A single cell spanning a multi-column grid is a pager or a
    footer, not data (Pagination mutation). Rows in the header band never count as data,
    whatever role or span their own cells carry (fix round 3, I1)."""
    all_rows = rows(page, grid)
    if band is None:
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


def first_data_row(
    page: PageSource, grid: HtmlElement, band: list[HtmlElement] | None = None
) -> HtmlElement | None:
    found = data_rows(page, grid, band)
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


def cell_under(
    page: PageSource, header: HtmlElement, band: list[HtmlElement] | None = None
) -> HtmlElement | None:
    """The first data row's cell in this header's column -- header-verified: the cell's own
    header, recomputed from the cell's position, must be this header. Declines outright
    (fix round 1, I1) when the grid has a rowspanned header-band cell anywhere: column position
    by colspan alone cannot be trusted to line up, and the verification below would only agree
    with the same miscounted position it computed the value from."""
    grid, index = grid_of(page, header), column_of(page, header)
    if grid is None or index is None:
        return None
    if band is None:
        band = header_band(page, grid)
    if header_has_rowspan(page, grid, band):
        return None
    row = first_data_row(page, grid, band)
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
