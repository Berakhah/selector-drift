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

# A table cell is never a row, whatever its siblings look like. Without this a wrapped
# single-field cell (<td><span>v</span></td>) would pick its own <td>, whose same-tag
# siblings are the other columns of the SAME row.
_NEVER_A_ROW = frozenset({"td", "th"})


def locate(root: HtmlElement, node_id: str) -> HtmlElement:
    """Find a static node_id by XPath and a live one by its data-sd-node attribute.

    Raises LookupError when it matches nothing or more than one node: an extraction must
    never silently read an empty page or an arbitrary twin."""
    hits = (
        root.xpath(node_id)
        if node_id.startswith("/")
        else root.xpath(f"//*[@{NODE_ATTR}=$n]", n=node_id)
    )
    if len(hits) != 1:
        raise LookupError(f"node {node_id!r} found {len(hits)} times")
    found: HtmlElement = hits[0]
    return found


def _has_same_tag_sibling(node: HtmlElement) -> bool:
    parent = node.getparent()
    return parent is not None and sum(1 for s in parent if s.tag == node.tag) >= 2


def row_of(el: HtmlElement, *, contains: Iterable[HtmlElement] = ()) -> HtmlElement:
    """The row container of a cell: the LOWEST ancestor of `el`, starting from its parent,
    that has at least one same-tag sibling (it is one of a list) and contains `el` and every
    element in `contains` (the other fields' cells). A <td>/<th> never qualifies.

    Raises LookupError when no ancestor qualifies, e.g. the fields sit in unrelated lists.

    Known limit: with ONE field and no other cell to constrain it, a wrapper that is itself
    repeated (a TableToDiv grid whose cells are wrapped in same-tag <div>s) is taken as the
    row. <td>/<th> wrappers are handled; arbitrary wrapper divs are not, and nothing here can
    tell them from rows. Multi-field specs are safe: the row must contain every field."""
    others = list(contains)
    node = el
    while (parent := node.getparent()) is not None:
        node = parent
        if (
            node.tag not in _NEVER_A_ROW
            and _has_same_tag_sibling(node)
            and all(o is node or node in o.iterancestors() for o in others)
        ):
            return node
    raise LookupError("no repeated ancestor holds every field: not inside one list of rows")


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
