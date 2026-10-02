"""Rung 1: the cached selector -- and the anchor that justified it, re-verified (design 5.2).

The most dangerous hole in a naive cache: columns are reordered, the cached selector still
matches exactly one element, its value still passes the format -- and rung 1 "succeeds" with
no heal recorded, so the strict path in Task 20 never engages. So a hit needs ALL of:

1. the selector matches exactly one VISIBLE element (H9) -- two is `ambiguous`;
2. every recorded piece of anchor evidence still lands on that element and on no other, read
   by the recording rung's own locate step: `accessible_name` via rung 2's `locate_by_name`,
   `label_text` via rung 3's `locate_by_label`, `header_text` via rung 4's `header_holds` (the
   element's band column header still reads it, and no other visible band header does) PLUS
   the position rung 4 itself only ever resolves to: a data cell of the grid's FIRST DATA ROW
   (grid.py's rules -- never the header band, a repeated header, a span-only row or a
   `<tfoot>` row), whose text is not the label. An entry with no anchor evidence (rung 4
   records `header_text=None` where column position cannot be trusted) verifies nothing and
   never hits;
3. the element's value passes `spec.matches()` (M23 -- `resolve/` may not import `validate/`);
4. the entry is for this spec version (M14): the cache is asked for exactly that key.

Rung 1 does not judge the VALUE beyond its format: Amount and Reserve text swapped under
untouched markup is a hit on the original node, and the gate's distributional check owns it.
"""

from __future__ import annotations

from lxml.html import HtmlElement

from selector_drift.resolve.aria import CELL_ROLES, DATA_CELL_ROLES
from selector_drift.resolve.cache import NO_CACHE, CachedSelector, CacheReader
from selector_drift.resolve.grid import first_data_row, grid_of, header_band, nearest, row_of
from selector_drift.resolve.labels import expected_label, normalise
from selector_drift.resolve.page import PageSource
from selector_drift.resolve.rungs.base import Family, Miss, ResolvedElement, RungBase, text_of
from selector_drift.resolve.rungs.r2_axtree import locate_by_name
from selector_drift.resolve.rungs.r3_anchors import locate_by_label
from selector_drift.resolve.rungs.r4_heuristics import header_holds
from selector_drift.spec.schema import FieldTarget, matches


def _first_record_cell(page: PageSource, el: HtmlElement) -> bool:
    """`el` is, or sits inside, a data cell of its grid's first data row -- the only position
    rung 4 ever resolves a header-anchored value to."""
    grid, cell = grid_of(page, el), nearest(page, el, CELL_ROLES)
    if grid is None or cell is None or page.role(cell) not in DATA_CELL_ROLES:
        return False
    row = row_of(page, cell)
    return row is not None and row is first_data_row(page, grid, header_band(page, grid))


def anchor_holds(
    page: PageSource, el: HtmlElement, cached: CachedSelector, target: FieldTarget
) -> str | None:
    """None if every recorded piece of anchor evidence still points at `el`; else why not.
    Each check re-runs the recording rung's own locate step, so 'holds' means 'the same
    evidence, read today, lands on this node and on no other'."""
    anchor = cached.anchor
    if anchor.is_empty():
        return "no anchor evidence was recorded, so there is nothing to re-verify"
    if anchor.accessible_name is not None:
        got = locate_by_name(page, anchor.accessible_name)
        if isinstance(got, Miss) or got[0] is not el:
            return f"accessible name {anchor.accessible_name!r} no longer labels this element"
    if anchor.label_text is not None:
        got = locate_by_label(page, anchor.label_text, target.relation)
        if isinstance(got, Miss) or got[0] is not el:
            return f"label {anchor.label_text!r} no longer anchors this element"
    if anchor.header_text is not None:
        if not header_holds(page, el, anchor.header_text):
            return f"header {anchor.header_text!r} no longer heads this element's column, uniquely"
        if not _first_record_cell(page, el):
            return f"the element under {anchor.header_text!r} is not a first-record data cell"
        text = normalise(text_of(el))
        if text == normalise(anchor.header_text) or text.startswith(expected_label(target)):
            return f"the element under {anchor.header_text!r} holds the label, not a value"
    return None


class CacheRung(RungBase):
    """Returns an element only if ALL hold: the selector matches exactly one visible element
    (H9); the stored anchor still verifies against it; its value passes spec.matches (M23);
    the entry is this spec version's (M14)."""

    rung_id: int = 1
    family: Family = Family.NONE

    def __init__(
        self, cache: CacheReader = NO_CACHE, *, portal: str = "", spec_version: int | None = None
    ) -> None:
        self.cache, self.portal, self.spec_version = cache, portal, spec_version

    def bind(self, cache: CacheReader, *, portal: str, spec_version: int | None) -> CacheRung:
        """The runner's CacheBound hook: a fresh rung per call, so a module-level list never
        holds a client's cache."""
        return CacheRung(cache, portal=portal, spec_version=spec_version)

    def attempt(self, page: PageSource, target: FieldTarget) -> ResolvedElement | Miss:
        cached = self.cache.get(self.portal, target.name, self.spec_version)
        if cached is None:
            return Miss("no_match", "no cache entry for this target, field and spec version")
        matched = page.query(cached.selector)
        if not matched:
            return Miss("no_match", "the cached selector matches nothing")
        shown = [el for el in matched if page.visible(el)]
        if not shown:
            return Miss("not_visible", "the cached selector matches only hidden elements")
        if len(shown) > 1:
            return Miss("ambiguous", f"the cached selector matches {len(shown)} visible elements")
        el = shown[0]
        broken = anchor_holds(page, el, cached, target)
        if broken is not None:
            return Miss("anchor_mismatch", broken)
        if not matches(target, text_of(el)):
            return Miss("no_match", "the element's value fails the spec's format assertion")
        return ResolvedElement(
            selector=cached.selector,
            node_id=page.node_id(el),
            anchor=cached.anchor,
            rung_id=self.rung_id,
        )


__all__ = ["CacheRung", "anchor_holds"]
