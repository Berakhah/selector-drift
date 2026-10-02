"""Rung 3: a visible text anchor and a structural relation from it.

Rung 3 anchors on visible TEXT, not on the accessibility tree -- that is what makes it a
different rung from 2 in the same family. The anchor is the deepest visible element whose whole
text is the label, widened to its ANCHOR BOX: the topmost ancestor-or-self whose whole text is
still the label (stopping below <body>). The box is an anchor only when it stands alone in its
parent: the parent carries no text of its own around it. So "Prior <b>Amount</b>" never anchors
`amount`, while "<div><span>Claim Number</span><span>AB12345678</span></div>" anchors the span.

Where HTML already defines the relation, the markup wins over the spec: a column header
relates to its column, a row header to its row, <dt> to its <dd>, <label> to its control -- and
only when that header / term / label itself spells the label as its whole text ("Prior Amount"
holding a span "Amount" is not an anchor for `amount`). FieldTarget.relation governs free-text
anchors only -- applied to a results-table header, spec v1's `right_of` would land on the next
header. A free-text anchor with no declared relation resolves nothing.

Grid rules are grid.py's, reused, never re-implemented (see its module docstring): a rowspan in
the header band declines with `anchor_mismatch`; a cell-derived value is never taken from the
header band, a repeated header, a span-only row or a <tfoot> row (`is_value_row`); a header
cell is never a value; a value whose own text is the label is never a value. Free-text
relations stay inside one row of a grid: RIGHT_OF is the immediately adjacent cell (never
skipping an empty one onto the next column), and BELOW / WITHIN across grid cells decline --
column position is rung 2's and rung 4's job, with header verification. When a rung cannot
verify, it declines.
"""

from __future__ import annotations

from lxml.html import HtmlElement

from selector_drift.resolve.aria import (
    CELL_ROLES,
    NON_RENDERED,
    TABLE_ROLES,
    ancestors,
    element_children,
    parent_of,
    tag_of,
)
from selector_drift.resolve.grid import (
    cell_right_of,
    cell_under,
    grid_of,
    header_band,
    header_has_rowspan,
    in_header_band,
    is_value_row,
    nearest,
    row_of,
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
from selector_drift.spec.schema import FieldTarget, Relation

_CONTROLS = frozenset({"input", "select", "textarea", "meter", "progress", "output"})
_HEADER_ROLES = frozenset({"columnheader", "rowheader"})
_ROW_LEVEL_ROLES = frozenset({"row", "rowgroup"}) | TABLE_ROLES
_SKIP_SIBLINGS = NON_RENDERED | {"br", "wbr"}
_TOP = frozenset({"body", "html"})


def _rendered(el: HtmlElement) -> bool:
    return (
        isinstance(el, HtmlElement) and isinstance(el.tag, str) and tag_of(el) not in (NON_RENDERED)
    )


def _label_elements(page: PageSource, want: str) -> list[HtmlElement]:
    """The deepest elements whose whole visible text is the label."""
    out: list[HtmlElement] = []
    for el in page.root().iter():
        if not isinstance(el, HtmlElement) or not _rendered(el):
            continue
        if any(tag_of(a) in NON_RENDERED for a in ancestors(el)):
            continue
        if normalise(text_of(el)) != want:
            continue
        if any(normalise(text_of(c)) == want for c in element_children(el)):
            continue
        out.append(el)
    return out


def _box(label: HtmlElement, want: str) -> HtmlElement:
    """The topmost ancestor-or-self whose whole text is still the label, below <body>."""
    box = label
    parent = parent_of(box)
    while parent is not None and tag_of(parent) not in _TOP and normalise(text_of(parent)) == want:
        box = parent
        parent = parent_of(box)
    return box


def _stands_alone(box: HtmlElement) -> bool:
    """The box's parent carries no text of its own around it (no 'Prior ' before 'Amount')."""
    parent = parent_of(box)
    if parent is None:
        return True
    loose = [parent.text or ""] + [c.tail or "" for c in parent]
    return not "".join(loose).strip()


def _container(box: HtmlElement, tag: str) -> HtmlElement | None:
    return next((a for a in [box, *ancestors(box)] if tag_of(a) == tag), None)


def _owns_text(el: HtmlElement) -> bool:
    """`el` holds non-whitespace text directly (its own text or a child's tail)."""
    loose = [el.text or ""] + [c.tail or "" for c in el]
    return bool("".join(loose).strip())


def _text_owners(candidates: list[HtmlElement]) -> list[HtmlElement]:
    return [
        el
        for el in candidates
        if _rendered(el)
        and not any(tag_of(a) in NON_RENDERED for a in ancestors(el))
        and (_owns_text(el) or tag_of(el) in _CONTROLS)
    ]


def _right_of(box: HtmlElement) -> HtmlElement | None:
    """The immediately next rendered sibling of the box -- never skipping an empty or hidden
    one onto the element after it (in a grid that would be the adjacent column)."""
    sib = box.getnext()
    while sib is not None and (not isinstance(sib, HtmlElement) or tag_of(sib) in _SKIP_SIBLINGS):
        sib = sib.getnext()
    return sib if isinstance(sib, HtmlElement) and isinstance(sib.tag, str) else None


def _free_relation(
    page: PageSource, box: HtmlElement, relation: Relation | None
) -> HtmlElement | None:
    in_grid = grid_of(page, box) is not None
    if relation is Relation.RIGHT_OF:
        return _right_of(box)
    if relation is Relation.BELOW:
        if in_grid:
            return None  # column position is rung 2/4's, with header verification
        following = [e for e in box.xpath("following::*") if isinstance(e, HtmlElement)]
        owners = _text_owners(following)
        return owners[0] if owners else None
    if relation is Relation.WITHIN:
        container = parent_of(box)
        if container is None or tag_of(container) in _TOP:
            return None
        if page.role(container) in _ROW_LEVEL_ROLES:
            return None  # "within" a row is right-of across cells -- not this relation
        inside = [
            e
            for e in container.iterdescendants()
            if isinstance(e, HtmlElement)
            and e is not box
            and not any(a is box for a in ancestors(e))
        ]
        owners = _text_owners(inside)
        return owners[0] if len(owners) == 1 else None  # more than one value: not a relation
    return None


class _Found:
    """value_for's verdict: the value, or why there is none."""

    __slots__ = ("direct", "rowspan", "value")

    def __init__(
        self, value: HtmlElement | None, *, direct: bool = False, rowspan: bool = False
    ) -> None:
        self.value = value
        self.direct = direct  # a <label>'s control: band check only, like rung 2's named controls
        self.rowspan = rowspan


_NONE = _Found(None)


def _relate(page: PageSource, label: HtmlElement, relation: Relation | None) -> _Found:
    want = normalise(text_of(label))
    if not want:
        return _NONE
    box = _box(label, want)
    if not _stands_alone(box):
        return _NONE
    header = nearest(page, box, _HEADER_ROLES)
    if header is not None:
        if normalise(text_of(header)) != want:
            return _NONE  # part of a longer header ("Prior Amount")
        grid = grid_of(page, header)
        band = header_band(page, grid) if grid is not None else []
        if page.role(header) == "columnheader":
            if grid is not None and header_has_rowspan(page, grid, band):
                return _Found(None, rowspan=True)
            return _Found(cell_under(page, header, band))
        return _Found(cell_right_of(page, header))
    term = _container(box, "dt")
    if term is not None:
        if normalise(text_of(term)) != want:
            return _NONE
        sib = term.getnext()
        while sib is not None and not (isinstance(sib, HtmlElement) and tag_of(sib) == "dd"):
            if isinstance(sib, HtmlElement) and tag_of(sib) == "dt":
                return _NONE  # this term has no definition of its own
            sib = sib.getnext()
        return _Found(sib if isinstance(sib, HtmlElement) else None)
    lab = _container(box, "label")
    if lab is not None:
        if normalise(text_of(lab)) != want:
            return _NONE
        target_id = lab.get("for")
        if target_id:
            hits = page.root().xpath("//*[@id=$i]", i=target_id)
            if (
                isinstance(hits, list)
                and len(hits) == 1
                and isinstance(hits[0], HtmlElement)
                and tag_of(hits[0]) in _CONTROLS
            ):
                return _Found(hits[0], direct=True)
            return _NONE
        controls = [d for d in lab.iterdescendants() if isinstance(d, HtmlElement)]
        controls = [d for d in controls if tag_of(d) in _CONTROLS]
        return _Found(controls[0], direct=True) if len(controls) == 1 else _NONE
    return _Found(_free_relation(page, box, relation))


def _acceptable(page: PageSource, found: _Found, want: str) -> bool:
    """The checks every value passes, whichever relation produced it (fail closed)."""
    value = found.value
    if value is None:
        return False
    if page.role(value) in _HEADER_ROLES:
        return False  # a header is never a value
    if normalise(text_of(value)) == want:
        return False  # nor is the label's own text ever stood in for the value
    grid = grid_of(page, value)
    if grid is None:
        return True
    band = header_band(page, grid)
    if found.direct:
        return not in_header_band(page, value, band)
    row = row_of(page, value)
    if row is None:
        return page.role(value) not in CELL_ROLES
    return is_value_row(page, row, band)


def value_for(
    page: PageSource, label: HtmlElement, relation: Relation | None
) -> HtmlElement | None:
    """The value `label` (an element whose whole text is a label) anchors, or None."""
    found = _relate(page, label, relation)
    return found.value if _acceptable(page, found, normalise(text_of(label))) else None


def locate_by_label(
    page: PageSource, label: str, relation: Relation | None
) -> tuple[HtmlElement, str] | Miss:
    """The one visible value the text `label` anchors, and the label as the page spells it.
    Also rung 1's re-verification of a stored label_text (Task 17)."""
    want = normalise(label)
    if not want:
        return Miss("no_match", "empty label")
    hits: list[tuple[HtmlElement, str]] = []
    saw_hidden = False
    rowspan_header = False
    for lab in _label_elements(page, want):
        if not page.visible(lab):
            saw_hidden = True  # H9: a hidden label anchors nothing
            continue
        found = _relate(page, lab, relation)
        rowspan_header = rowspan_header or found.rowspan
        value = found.value
        if value is None or not _acceptable(page, found, want):
            continue
        if not page.visible(value):
            saw_hidden = True
            continue
        hits.append((value, text_of(lab)))
    values = unique([v for v, _ in hits])
    if not values:
        if rowspan_header:
            return Miss("anchor_mismatch", f"rowspan header: cannot verify column for {label!r}")
        return Miss(
            "not_visible" if saw_hidden else "no_match",
            f"no visible value is anchored by the text {label!r}",
        )
    if len(values) > 1:
        return Miss("ambiguous", f"the text {label!r} anchors {len(values)} values")
    return values[0], next(t for v, t in hits if v is values[0])


class AnchorRung(RungBase):
    rung_id: int = 3
    family: Family = Family.TEXT_SEMANTIC

    def attempt(self, page: PageSource, target: FieldTarget) -> ResolvedElement | Miss:
        found = locate_by_label(page, expected_label(target), target.relation)
        if isinstance(found, Miss):
            return found
        el, spelled = found
        return self.found(page, el, AnchorEvidence(label_text=spelled))


__all__ = ["AnchorRung", "locate_by_label", "value_for"]
