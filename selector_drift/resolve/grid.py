"""Tables and grids by role, so a native <table> and TableToDiv's role="table" divs are one model.

Column positions count colspan (aria-colspan on divs). Which rows are header and which are data
is decided here, once, by three rules (fix round 4):

1. The HEADER BAND (`header_band()`) is every row from the top through the end of the FIRST
   contiguous run of rows that contain a `columnheader` cell. A row with no columnheader that
   sits above that run is in the band by position (a `<td colspan>` title row; probe P4's
   all-`<td>` grouping row with its rowspanned corner). A row with no columnheader that sits
   *inside* the run, between two columnheader rows, is in the band too when it is span-only
   (every cell spans more than one column) or one of its cells rowspans down into the next
   columnheader row -- that is header structure, not a record. Any other row ends the run. No
   `columnheader` anywhere on the grid -> an empty band (a row-header table is not swept in by
   a span).
2. DATA ROWS (`data_rows()`) are rows below the band, EXCLUDING (i) any row that contains a
   `columnheader` (a header row repeated mid-table, a `<tbody>` `<th colspan>` section label)
   and (ii) any row in which every cell spans more than one column (a group, sub-header or
   divider row; this generalises the full-width pager skip). `first_data_row()` is the first
   row passing both. A value is never taken from an excluded row or from the band
   (`is_value_row()` is the per-row check for paths -- `cell_right_of()`, a directly named
   cell -- that do not go through `data_rows()`).
3. `header_has_rowspan()` scans only the band: a `rowspan`/`aria-rowspan` > 1 there means
   colspan-only column position cannot be trusted to line up, so `cell_under()` declines
   (rung 2 reports `Miss("anchor_mismatch", "rowspan header ...")`). A rowspan in a data row
   (grouped line items) is an ordinary data shape and is not this.

Round 3 ended the band at the LAST columnheader row, so a header repeated mid-table swept the
first records into the band and a field silently resolved to a later record; round 2 keyed the
band on spans and swept genuine data rows in. All band-taking helpers accept an optional
pre-computed `band`, so a caller resolving several candidates against one grid computes it once.
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


def _has_columnheader(page: PageSource, row: HtmlElement) -> bool:
    return any(page.role(c) == "columnheader" for c in cells(page, row))


def _rowspan(cell: HtmlElement) -> int:
    raw = cell.get("rowspan") or cell.get("aria-rowspan") or "1"
    return min(max(int(raw), 1), 1000) if raw.isdigit() else 1


def _max_rowspan(page: PageSource, row: HtmlElement) -> int:
    return max((_rowspan(c) for c in cells(page, row)), default=1)


def is_span_only_row(page: PageSource, row: HtmlElement) -> bool:
    """Every cell spans more than one column: a group, sub-header or divider row."""
    cs = cells(page, row)
    return bool(cs) and all(span(c) > 1 for c in cs)


def header_band(page: PageSource, grid: HtmlElement) -> list[HtmlElement]:
    """Rows from the top through the end of the FIRST contiguous run of columnheader rows
    (rule 1 of the module docstring). Rows above the run belong to it by position. Rows with
    no columnheader that sit between two of its columnheader rows belong to it when each is
    span-only or rowspans down into the next columnheader row (header structure, not a
    record); any other row ends the run. No columnheader anywhere -> empty band."""
    all_rows = rows(page, grid)
    flags = [_has_columnheader(page, r) for r in all_rows]
    if True not in flags:
        return []
    end = flags.index(True)
    while True:
        nxt = next((k for k in range(end + 1, len(all_rows)) if flags[k]), None)
        if nxt is None:
            break
        gap = range(end + 1, nxt)
        if not all(
            is_span_only_row(page, all_rows[g]) or g + _max_rowspan(page, all_rows[g]) - 1 >= nxt
            for g in gap
        ):
            break
        end = nxt
    return all_rows[: end + 1]


def is_value_row(page: PageSource, row: HtmlElement, band: list[HtmlElement] | None = None) -> bool:
    """False for a band row and for a row excluded from data by rule 2: one that contains a
    columnheader, or one in which every cell spans more than one column."""
    if band is None:
        grid = grid_of(page, row)
        band = header_band(page, grid) if grid is not None else []
    if any(row is b for b in band):
        return False
    return not _has_columnheader(page, row) and not is_span_only_row(page, row)


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
    """True when any cell in the header band (rule 3: the band only) spans multiple rows.
    `column_of` and `cell_at` count colspan only, so a rowspanned band cell desynchronises
    column indices between band rows and the data row beneath, and position alone can no longer
    be trusted to verify a header. A rowspan in a *data* row (grouped line items) is not this."""
    for r in header_band(page, grid) if band is None else band:
        if _max_rowspan(page, r) > 1:
            return True
    return False


def data_rows(
    page: PageSource, grid: HtmlElement, band: list[HtmlElement] | None = None
) -> list[HtmlElement]:
    """Visible rows below the header band holding data (rule 2): never a band row, a row that
    contains a columnheader, or a row in which every cell spans more than one column. A single
    cell in a multi-column grid is a pager or footer, not data (Pagination mutation)."""
    all_rows = rows(page, grid)
    if band is None:
        band = header_band(page, grid)
    columns = max((width(page, r) for r in all_rows), default=0)
    out = []
    for r in all_rows:
        if not is_value_row(page, r, band) or not page.visible(r):
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


def column_header(
    page: PageSource, cell: HtmlElement, band: list[HtmlElement] | None = None
) -> HtmlElement | None:
    """The header a cell sits under: its headers="" idrefs first, else by column position.
    Given a `band`, position is read only from header rows inside it (fix round 4): a header
    row repeated mid-table -- possibly in another column order -- never verifies a cell."""
    for ident in (cell.get("headers") or "").split():
        hits = page.root().xpath("//*[@id=$i]", i=ident)
        if isinstance(hits, list):
            for h in hits:
                if isinstance(h, HtmlElement) and page.role(h) == "columnheader":
                    return h
    grid, index = grid_of(page, cell), column_of(page, cell)
    if grid is None or index is None:
        return None
    candidates = header_rows(page, grid)
    if band is not None:
        candidates = [hr for hr in candidates if any(hr is b for b in band)]
    for hr in reversed(candidates):
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
    return cell if column_header(page, cell, band) is header else None


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
    "is_span_only_row",
    "is_value_row",
    "nearest",
    "row_header",
    "row_of",
    "rows",
    "span",
    "width",
]
