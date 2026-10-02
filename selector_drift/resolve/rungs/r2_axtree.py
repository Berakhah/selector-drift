"""Rung 2: role + accessible name + relationship, read from the accessibility tree."""

from __future__ import annotations

from lxml.html import HtmlElement

from selector_drift.resolve.grid import (
    cell_right_of,
    cell_under,
    grid_of,
    header_band,
    header_has_rowspan,
    in_header_band,
)
from selector_drift.resolve.labels import expected_label, normalise
from selector_drift.resolve.page import PageSource
from selector_drift.resolve.rungs.base import (
    AnchorEvidence,
    Family,
    Miss,
    ResolvedElement,
    RungBase,
    text_of,
    unique,
)
from selector_drift.spec.schema import FieldTarget

# Roles whose accessible name may come from an element's own content (resolve.aria's
# NAME_FROM_CONTENT) and that also appear in VALUE_ROLES. For these, a name match is only
# trustworthy when an author put it there explicitly (fix round 1, I2) -- otherwise a <td>
# whose text happens to equal the label resolves as if it were its own value.
_CONTENT_NAMED_VALUE_ROLES = frozenset({"cell", "gridcell"})

# Roles whose own accessible name labels the value they hold.
VALUE_ROLES = frozenset(
    {
        "textbox",
        "searchbox",
        "combobox",
        "spinbutton",
        "listbox",
        "cell",
        "gridcell",
        "definition",
        "status",
        "meter",
        "progressbar",
        "slider",
    }
)


def _author_name(el: HtmlElement, page: PageSource) -> str:
    """The name an author explicitly put on `el` via aria-label/aria-labelledby -- never the
    name computed from its own content (fix round 1, I2)."""
    label = (el.get("aria-label") or "").strip()
    if label:
        return label
    texts: list[str] = []
    for ident in (el.get("aria-labelledby") or "").split():
        hits = page.root().xpath("//*[@id=$i]", i=ident)
        if isinstance(hits, list):
            texts.extend(text_of(h) for h in hits if isinstance(h, HtmlElement))
    return " ".join(t for t in texts if t)


def _hidden_candidate_exists(page: PageSource, want: str) -> bool:
    """I3: `page.ax().walk()` prunes aria-hidden / display:none / `[hidden]` subtrees outright
    (Task 13), so a page whose only candidate is hidden that way never reaches the walk above
    and would otherwise be reported as `no_match` instead of `not_visible`. Scan the raw DOM
    directly -- an author-named element, or for a header role one named by its own text --
    that `page.visible()` says is not visible."""
    for el in page.root().iter():
        if not isinstance(el, HtmlElement) or not isinstance(el.tag, str) or page.visible(el):
            continue
        name = _author_name(el, page)
        if not name and page.role(el) in ("columnheader", "rowheader"):
            name = text_of(el)
        if name and normalise(name) == want:
            return True
    return False


def locate_by_name(page: PageSource, name: str) -> tuple[HtmlElement, str] | Miss:
    """The one visible element that `name` labels, and the name as the page spells it.
    Also rung 1's re-verification of a stored accessible_name (Task 17)."""
    want = normalise(name)
    hits: list[tuple[HtmlElement, str]] = []
    rowspan_header = False
    bands: dict[int, list[HtmlElement]] = {}  # fix round 3: one header_band() per grid per call

    def band_of(grid: HtmlElement) -> list[HtmlElement]:
        key = id(grid)
        if key not in bands:
            bands[key] = header_band(page, grid)
        return bands[key]

    for node in page.ax().walk():
        if not want or normalise(node.name) != want:
            continue
        anchor = page.node(node.node_id)
        if anchor is None:
            continue
        if not page.visible(anchor):
            continue  # H9 -- a genuinely hidden-only page is diagnosed below (I3)
        if node.role in _CONTENT_NAMED_VALUE_ROLES and not _author_name(anchor, page):
            continue  # I2: a label cell is never taken as its own value
        grid = grid_of(page, anchor)
        band = band_of(grid) if grid is not None else []
        if node.role == "columnheader":
            if grid is not None and header_has_rowspan(page, grid, band):
                rowspan_header = True  # I1: column position can't be trusted -- decline
                continue
            value = cell_under(page, anchor, band)
        elif node.role == "rowheader":
            value = cell_right_of(page, anchor)
        elif node.role in VALUE_ROLES:
            value = anchor
        else:
            continue
        if value is None or not page.visible(value):
            continue
        if normalise(text_of(value)) == want:
            continue  # I2: nor is the label's own text ever stood in for the value
        # `value` is always inside the same grid as `anchor` (cell_under/cell_right_of stay
        # within it; the VALUE_ROLES branch sets value = anchor), so the cached band applies.
        if in_header_band(page, value, band if grid is not None else None):
            continue  # I1 (fix round 3): a header-band cell is never a value, whichever
            # path produced it -- cell_right_of walks a single row and does not go through
            # data_rows(), so it is not otherwise stopped from handing back a band cell
        hits.append((value, node.name))
    values = unique([v for v, _ in hits])
    if not values:
        if rowspan_header:
            return Miss("anchor_mismatch", f"rowspan header: cannot verify column for {name!r}")
        if _hidden_candidate_exists(page, want):
            return Miss("not_visible", f"no visible element is named {name!r}")
        return Miss("no_match", f"no visible element is named {name!r}")
    if len(values) > 1:
        return Miss("ambiguous", f"{len(values)} elements are named {name!r}")
    return values[0], next(n for v, n in hits if v is values[0])


class AxTreeRung(RungBase):
    rung_id: int = 2
    family: Family = Family.TEXT_SEMANTIC

    def attempt(self, page: PageSource, target: FieldTarget) -> ResolvedElement | Miss:
        found = locate_by_name(page, expected_label(target))
        if isinstance(found, Miss):
            return found
        el, spelled = found
        return self.found(page, el, AnchorEvidence(accessible_name=spelled))


__all__ = ["AxTreeRung", "locate_by_name"]
