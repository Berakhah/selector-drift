"""Rung 4: attribute and structural heuristics -- the STRUCTURAL family.

In order: a test id naming the field; a stable id / name / class naming the field; the column
under the header that names the field, header-verified. No strategy reads visible label text
except through the header, so a label reword does not defeat the first two. Never reads a
data-sd-* attribute: those are ours (node ids) and the bench's (truth marks).

"Names the field" is token equality after dropping AFFIXES from either END only (`col-`,
`-value`): `col-claim_number` names `claim_number`, `prior-claim-number` and
`claim-col-number` do not, and a field whose own name holds an affix word (`total_value`)
still matches. A generated id (UNSTABLE_ID) is never evidence -- it is what IdChurn churns.

Grid rules are grid.py's, reused, never re-implemented (see its module docstring):

* An attribute match inside a grid counts only when it is, or sits inside, a data cell of the
  grid's FIRST DATA ROW (`first_data_row` with the header band): never the header band, a
  repeated header, a span-only row, a `<tfoot>` row, a header cell or a row/rowgroup itself,
  and never a later record when the first record carries none (that is another record, not a
  heal). Two matches in that row -- DecoyColumn's cloned classes, DuplicateLabel's legacy
  column -- are `ambiguous`, never first-by-position. Matches in two visible grids, or a grid
  and a loose element, are `ambiguous` too.
* A value is never a <label>, <dt>, <legend> or <caption> (nor inside one, except a label's
  own control), and never an element whose whole text is the field's label.
* The header strategy uses band headers only (a header repeated mid-table never anchors),
  declines `anchor_mismatch` on a rowspan in the band, and recomputes the chosen cell's own
  header (colspans counted, headers="" honoured) -- a different header is `anchor_mismatch`.
* `header_text` is the resolved cell's band header, whichever strategy won -- or None when the
  band has a rowspan and the cell carries no headers="" (position cannot be trusted there).

Deliberate: a test id outranks a class and a header (the brief's order), and an ambiguous
attribute strategy still lets the independently verified header strategy speak.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from lxml.html import HtmlElement

from selector_drift.resolve.aria import CELL_ROLES, DATA_CELL_ROLES, ancestors, tag_of
from selector_drift.resolve.grid import (
    cell_at,
    column_header,
    column_of,
    first_data_row,
    grid_of,
    header_band,
    header_has_rowspan,
    in_header_band,
    nearest,
    row_of,
)
from selector_drift.resolve.labels import expected_label, field_tokens, normalise, tokens
from selector_drift.resolve.page import PageSource
from selector_drift.resolve.rungs.base import (
    AnchorEvidence,
    Family,
    Miss,
    ResolvedElement,
    RungBase,
    text_of,
)
from selector_drift.spec.schema import FieldTarget

TESTID_ATTRS = ("data-testid", "data-test-id", "data-test", "data-qa", "data-cy")
AFFIXES = frozenset({"col", "column", "cell", "field", "value", "val", "td", "th", "data", "js"})
# Generated ids churn by design (IdChurn); an id that looks generated is not evidence.
UNSTABLE_ID = re.compile(
    r"\d{3,}|[0-9a-f]{6,}|^:r|^ctl\d+_|^(?:ember|ng-|react-|mui-|css-|sc-|jsx-)", re.IGNORECASE
)

_LABEL_TAGS = frozenset({"label", "dt", "legend", "caption"})
_CONTROLS = frozenset({"input", "select", "textarea", "output", "meter", "progress"})
_HEADER_ROLES = frozenset({"columnheader", "rowheader"})


def names_field(value: str, want: tuple[str, ...]) -> bool:
    """`want` appears as a contiguous token run with only AFFIXES before and after it."""
    got, n = tokens(value), len(want)
    if not n:
        return False
    for i in range(len(got) - n + 1):
        if got[i : i + n] == want and all(t in AFFIXES for t in got[:i] + got[i + n :]):
            return True
    return False


def _elements(page: PageSource) -> list[HtmlElement]:
    return [
        el for el in page.root().iter() if isinstance(el, HtmlElement) and isinstance(el.tag, str)
    ]


class _Grids:
    """header_band / first_data_row once per grid per attempt (keyed on the element itself,
    as rung 2 does: the dict keeps the lxml proxy alive)."""

    def __init__(self, page: PageSource) -> None:
        self.page = page
        self._bands: dict[HtmlElement, list[HtmlElement]] = {}
        self._first: dict[HtmlElement, HtmlElement | None] = {}

    def band(self, grid: HtmlElement) -> list[HtmlElement]:
        if grid not in self._bands:
            self._bands[grid] = header_band(self.page, grid)
        return self._bands[grid]

    def first_row(self, grid: HtmlElement) -> HtmlElement | None:
        if grid not in self._first:
            self._first[grid] = first_data_row(self.page, grid, self.band(grid))
        return self._first[grid]

    def header_of(self, el: HtmlElement) -> HtmlElement | None:
        """The band header the element's cell sits under; None where position is untrusted."""
        cell = nearest(self.page, el, CELL_ROLES)
        grid = grid_of(self.page, el)
        if cell is None or grid is None:
            return None
        band = self.band(grid)
        if not cell.get("headers") and header_has_rowspan(self.page, grid, band):
            return None
        return column_header(self.page, cell, band)


def _label_like(el: HtmlElement) -> bool:
    if tag_of(el) in _LABEL_TAGS:
        return True
    inside = [a for a in ancestors(el) if tag_of(a) in _LABEL_TAGS]
    return bool(inside) and not (tag_of(el) in _CONTROLS and tag_of(inside[0]) == "label")


def _innermost(found: list[HtmlElement]) -> list[HtmlElement]:
    """Drop any match that contains another match: the inner one is the more specific."""
    out: list[HtmlElement] = []
    for el in found:
        if any(o is not el and any(a is el for a in ancestors(o)) for o in found):
            continue
        if not any(o is el for o in out):
            out.append(el)
    return out


def _pick(
    page: PageSource, grids: _Grids, found: list[HtmlElement], want: str, what: str
) -> HtmlElement | Miss | None:
    """The one visible value the matches point at, or why there is none."""
    if not found:
        return None
    visible = [el for el in found if page.visible(el)]
    if not visible:
        return Miss("not_visible", f"every element whose {what} names the field is hidden")
    # (grid or None, element): one entry per grid that offers a first-record value.
    offers: list[tuple[HtmlElement | None, HtmlElement]] = []
    later_records = False
    by_grid: dict[HtmlElement, list[HtmlElement]] = {}
    for el in _innermost(visible):
        if _label_like(el) or page.role(el) in _HEADER_ROLES:
            continue
        if normalise(text_of(el)) == want:
            continue  # a label's own text is never its value
        grid, row = grid_of(page, el), row_of(page, el)
        if grid is None or row is None:
            if grid is None:
                offers.append((None, el))
            continue  # inside a grid but outside its rows (a caption): never a value
        cell = nearest(page, el, CELL_ROLES)
        if cell is None or page.role(cell) not in DATA_CELL_ROLES or row_of(page, cell) is not row:
            continue  # the row or rowgroup itself, or a header cell's content
        if in_header_band(page, cell, grids.band(grid)):
            continue
        if row is not grids.first_row(grid):
            later_records = True  # a later record, a <tfoot>, a repeated header: not this one
            continue
        by_grid.setdefault(grid, []).append(el)
    for grid, els in by_grid.items():
        if len(els) > 1:
            return Miss("ambiguous", f"{len(els)} cells in the first record have that {what}")
        offers.append((grid, els[0]))
    if len(offers) > 1:
        return Miss("ambiguous", f"{len(offers)} separate elements have that {what}")
    if offers:
        return offers[0][1]
    if later_records:
        return Miss("no_match", f"only a later record or a footer row has that {what}")
    return None


def _by_attributes(
    page: PageSource, target: FieldTarget, grids: _Grids
) -> HtmlElement | Miss | None:
    want = field_tokens(target)
    label = expected_label(target)
    everything = _elements(page)
    by_testid = [
        el for el in everything if any(names_field(el.get(a) or "", want) for a in TESTID_ATTRS)
    ]
    picked = _pick(page, grids, by_testid, label, "test id")
    if picked is not None:
        return picked

    def stable(el: HtmlElement) -> bool:
        ident = el.get("id") or ""
        if ident and not UNSTABLE_ID.search(ident) and names_field(ident, want):
            return True
        if names_field(el.get("name") or "", want):
            return True
        return any(names_field(c, want) for c in (el.get("class") or "").split())

    return _pick(page, grids, [el for el in everything if stable(el)], label, "id/name/class")


def _band_headers(page: PageSource, grids: _Grids, want: str) -> tuple[list[HtmlElement], bool]:
    """Column headers in their grid's header band whose accessible name is `want`, and
    whether a hidden one exists (for the not_visible diagnosis)."""
    out: list[HtmlElement] = []
    hidden = False
    for el in _elements(page):
        if page.role(el) != "columnheader":
            continue
        if not page.visible(el):
            # A hidden node has no accessible name; its text is enough for the diagnosis.
            hidden = hidden or normalise(page.name(el) or text_of(el)) == want
            continue
        if normalise(page.name(el)) != want:
            continue
        grid = grid_of(page, el)
        if grid is not None and in_header_band(page, el, grids.band(grid)):
            out.append(el)
    return out, hidden


def _by_header(page: PageSource, target: FieldTarget, grids: _Grids) -> HtmlElement | Miss | None:
    want = expected_label(target)
    headers, hidden = _band_headers(page, grids, want)
    if not headers:
        return Miss("not_visible", f"only a hidden header reads {want!r}") if hidden else None
    if len(headers) > 1:
        return Miss("ambiguous", f"{len(headers)} visible headers read {want!r}")
    header = headers[0]
    grid, index = grid_of(page, header), column_of(page, header)
    if grid is None or index is None:
        return None
    band = grids.band(grid)
    if header_has_rowspan(page, grid, band):
        return Miss("anchor_mismatch", f"rowspan header: cannot verify column for {want!r}")
    row = grids.first_row(grid)
    if row is None:
        return Miss("no_match", f"the grid under {want!r} has no data row")
    cell = cell_at(page, row, index)
    if cell is None or page.role(cell) not in DATA_CELL_ROLES:
        return Miss("no_match", f"the first data row has no data cell at column {index}")
    if column_header(page, cell, band) is not header:
        return Miss(
            "anchor_mismatch",
            f"header verification: the cell at column {index} sits under another header",
        )
    if not page.visible(cell):
        return Miss("not_visible", f"the cell under {want!r} is hidden")
    if normalise(text_of(cell)) == want:
        return Miss("no_match", f"the cell under {want!r} holds only the label")
    return cell


_Strategy = Callable[[PageSource, FieldTarget, _Grids], HtmlElement | Miss | None]
_STRATEGIES: tuple[_Strategy, ...] = (_by_attributes, _by_header)


class HeuristicRung(RungBase):
    rung_id: int = 4
    family: Family = Family.STRUCTURAL

    def attempt(self, page: PageSource, target: FieldTarget) -> ResolvedElement | Miss:
        grids = _Grids(page)
        refusal: Miss | None = None
        for strategy in _STRATEGIES:
            got = strategy(page, target, grids)
            if isinstance(got, Miss):
                refusal = refusal or got
                continue
            if got is not None:
                header = grids.header_of(got)
                text = page.name(header).strip() if header is not None else None
                return self.found(page, got, AnchorEvidence(header_text=text or None))
        return refusal or Miss("no_match", f"no structural signal names {target.name!r}")


def header_holds(page: PageSource, el: HtmlElement, header_text: str) -> bool:
    """Rung 1's re-verification of a stored header_text: the element's band column header
    still reads it, and no other visible band header on the page does (DuplicateLabel)."""
    grids = _Grids(page)
    header = grids.header_of(el)
    want = normalise(header_text)
    if header is None or normalise(page.name(header)) != want:
        return False
    found, _ = _band_headers(page, grids, want)
    return len(found) == 1


__all__ = ["AFFIXES", "TESTID_ATTRS", "UNSTABLE_ID", "HeuristicRung", "header_holds", "names_field"]
