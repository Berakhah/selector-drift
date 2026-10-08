"""The row model: how one resolved cell per field becomes N rows, deterministically.

The cascade resolves each field to ONE node (its cell in the first data row). These helpers
generalise that to every row without judging a value: the gate does that. They work on a
parsed DOM string, so a `<table>` and a `TableToDiv` grid take the same path.
"""

from __future__ import annotations

from collections.abc import Iterable

from lxml.html import HtmlElement

from selector_drift.resolve.page import NODE_ATTR

type RelPath = tuple[tuple[str, int], ...]

# A cell is never a row, whatever its siblings look like: by tag (td/th) or by ARIA role (what
# TableToDiv emits). Without this a wrapped cell would be taken as its own row, whose
# same-tag siblings are the other columns of the SAME row.
_CELL_TAGS = frozenset({"td", "th"})
_CELL_ROLES = frozenset({"cell", "gridcell", "columnheader", "rowheader"})
_ROW_TAG = "tr"
_ROW_ROLE = "row"


def locate(root: HtmlElement, node_id: str) -> HtmlElement:
    """Find a static node_id by XPath and a live one by its data-sd-node attribute.

    Raises LookupError when it matches nothing, more than one node, or a non-element (an
    attribute or text result): an extraction must never silently read an empty page or an
    arbitrary twin."""
    found = (
        root.xpath(node_id)
        if node_id.startswith("/")
        else root.xpath(f"//*[@{NODE_ATTR}=$n]", n=node_id)
    )
    hits = found if isinstance(found, list) else []
    if len(hits) != 1 or not isinstance(hits[0], HtmlElement):
        raise LookupError(f"node {node_id!r} is not exactly one element ({len(hits)} hits)")
    hit: HtmlElement = hits[0]
    return hit


def _is_cell(node: HtmlElement) -> bool:
    return node.tag in _CELL_TAGS or (node.get("role") or "") in _CELL_ROLES


def _has_same_tag_sibling(node: HtmlElement) -> bool:
    parent = node.getparent()
    return parent is not None and sum(1 for s in parent if s.tag == node.tag) >= 2


def _is_explicit_row(node: HtmlElement) -> bool:
    return bool(node.tag == _ROW_TAG or node.get("role") == _ROW_ROLE)


def row_of(el: HtmlElement, *, contains: Iterable[HtmlElement] = ()) -> HtmlElement:
    """The row container of a cell, found walking up from `el`'s parent (cells skipped:
    td/th or role cell/gridcell/columnheader/rowheader).

    An EXPLICIT row (a <tr> or [role=row]) is the row whether or not it has siblings, so a
    one-result table is a table of one row and a following footer or second table is never
    zipped in. Otherwise the row is the first ancestor with a same-tag sibling. Either way
    there is no further climbing: a higher repeated ancestor (a layout <div>) that holds two
    unrelated cells is not a row.

    Raises LookupError when there is no such ancestor; when it does not contain every element
    in `contains` (the other fields' cells: they do not share a row); or when a non-explicit
    row's own parent is also a repeated level (a wrapper or group <div> inside a repeated row
    <div>), which cannot be told from a real row and so is refused rather than guessed.

    Rows before the anchor row, and rows in other row groups (a second <tbody>, a tfoot), are
    not part of the result: extraction drops them by position and Task 29's row-count
    contract is the net for a table that really spans groups."""
    others = [o for o in contains if o is not el]
    node = el
    while (parent := node.getparent()) is not None:
        node = parent
        explicit = _is_explicit_row(node)
        if _is_cell(node) or not (explicit or _has_same_tag_sibling(node)):
            continue
        if not all(node in o.iterancestors() for o in others):
            raise LookupError("the fields do not share the anchor's row")
        container = node.getparent()
        if not explicit and container is not None and _has_same_tag_sibling(container):
            raise LookupError("the row is ambiguous (nested repeated levels)")
        return node
    raise LookupError("no repeated ancestor: not inside a list of rows")


def rel_path(row: HtmlElement, el: HtmlElement) -> RelPath:
    steps: list[tuple[str, int]] = []
    node = el
    while node is not row:
        parent = node.getparent()
        if parent is None:
            raise LookupError("element is not inside the given row")
        same = [s for s in parent if s.tag == node.tag]
        steps.append((str(node.tag), same.index(node)))
        node = parent
    return tuple(reversed(steps))


def follow(row: HtmlElement, path: RelPath) -> HtmlElement | None:
    node = row
    for tag, index in path:
        same = [c for c in node if c.tag == tag]
        if index >= len(same):
            return None
        node = same[index]
    return node
