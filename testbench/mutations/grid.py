"""The lxml helpers every mutation class shares.

A day applies one or two benign classes in a random order and then, some days, one adversarial
class on top. So every class must find the results grid whatever markup earlier classes left
behind: a <table>, or TableToDiv's role-annotated <div>s, with cells possibly wrapped by
DomRestructure and relabelled by LabelReword. Nothing here reads a data-sd-* attribute; the one
write is clone(), which strips them from a copy so a decoy never carries the answer key's marker.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable, Sequence

from lxml.html import HtmlElement

GRID_XPATH = '//table | //*[@role="table" or @role="grid"]'
ROW_XPATH = './/tr | .//*[@role="row"]'
HEADER_CELL_XPATH = './th | ./*[@role="columnheader"]'
DATA_CELL_XPATH = './td | ./*[@role="cell" or @role="gridcell"]'
BENCH_ONLY_PREFIX = "data-sd-"


def elements(found: object) -> list[HtmlElement]:
    """An XPath result, narrowed to elements (comments and strings dropped)."""
    if not isinstance(found, list):
        return []
    return [node for node in found if isinstance(node, HtmlElement)]


def grids(doc: HtmlElement) -> list[HtmlElement]:
    return elements(doc.xpath(GRID_XPATH))


def rows(grid: HtmlElement) -> list[HtmlElement]:
    return elements(grid.xpath(ROW_XPATH))


def header_cells(row: HtmlElement) -> list[HtmlElement]:
    return elements(row.xpath(HEADER_CELL_XPATH))


def data_cells(row: HtmlElement) -> list[HtmlElement]:
    return elements(row.xpath(DATA_CELL_XPATH))


def header_row(grid: HtmlElement) -> HtmlElement | None:
    return next((r for r in rows(grid) if header_cells(r) and not data_cells(r)), None)


def data_rows(grid: HtmlElement) -> list[HtmlElement]:
    return [r for r in rows(grid) if data_cells(r)]


def text_of(el: HtmlElement) -> str:
    """Accessible text: aria-label, then an <img>'s alt, then descendant text, whitespace
    collapsed. The same reading as tests.testbench.helpers.accessible_text, so a mutation
    finds a header by exactly the text a test (and a screen reader) sees."""
    label = el.get("aria-label")
    if label:
        return " ".join(label.split())
    if el.tag == "img":
        return " ".join((el.get("alt") or "").split())
    parts = [el.text or ""]
    for child in el:
        if isinstance(child, HtmlElement):
            parts.append(" " + text_of(child) + " ")
        parts.append(child.tail or "")
    return " ".join("".join(parts).split())


def innermost(el: HtmlElement) -> HtmlElement:
    """The element that actually holds a cell's text: descend through single-child wrappers
    (DomRestructure's span, the claim-number link) until the text is found."""
    node = el
    while True:
        kids = [c for c in node if isinstance(c, HtmlElement)]
        if (
            len(kids) != 1
            or kids[0].tag == "img"
            or (node.text or "").strip()
            or (kids[0].tail or "").strip()
        ):
            return node
        node = kids[0]


def set_text(el: HtmlElement, value: str) -> None:
    """Replace a cell's visible text, keeping every wrapper and attribute around it."""
    node = innermost(el)
    for child in list(node):
        node.remove(child)
    node.text = value


def clone(el: HtmlElement, *, id_suffix: str) -> HtmlElement:
    """A deep copy with every data-sd-* attribute removed (Task 8's rule for clones) and every
    id suffixed, so the copy can never be mistaken for the true cell or collide with it."""
    twin = copy.deepcopy(el)
    twin.tail = None
    for node in twin.iter():
        if not isinstance(node, HtmlElement):
            continue
        for attr in [a for a in node.attrib if a.startswith(BENCH_ONLY_PREFIX)]:
            del node.attrib[attr]
        ident = node.get("id")
        if ident:
            node.set("id", f"{ident}{id_suffix}")
    return twin


def wrap(el: HtmlElement, tag: str, attrs: dict[str, str]) -> HtmlElement:
    """Put `el` inside a new element, leaving el's tail text outside it."""
    wrapper = el.makeelement(tag, attrs)
    wrapper.tail, el.tail = el.tail, None
    el.addprevious(wrapper)
    wrapper.append(el)
    return wrapper


def wrap_contents(el: HtmlElement, tag: str, attrs: dict[str, str]) -> HtmlElement:
    """Move el's text and children into a new single child of el. el keeps its attributes."""
    inner = el.makeelement(tag, attrs)
    inner.text, el.text = el.text, None
    for child in list(el):
        inner.append(child)
    el.append(inner)
    return inner


def detach(el: HtmlElement) -> None:
    """Remove el from its parent without losing the text that followed it."""
    parent = el.getparent()
    if parent is None:
        return
    if el.tail:
        previous = el.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    parent.remove(el)


def permute(cells: Sequence[HtmlElement], order: Sequence[int]) -> None:
    """Reorder sibling cells in place: position i receives cells[order[i]]. Whitespace between
    cells stays where it was. Cells that do not share one parent are left alone."""
    if len(cells) != len(order) or not cells:
        return
    parent = cells[0].getparent()
    if parent is None or any(c.getparent() is not parent for c in cells):
        return
    slots: list[HtmlElement] = []
    for cell in cells:
        slot = cell.makeelement("template", {})
        slot.tail, cell.tail = cell.tail, None
        parent.replace(cell, slot)
        slots.append(slot)
    for slot, index in zip(slots, order, strict=True):
        moved = cells[index]
        moved.tail = slot.tail
        parent.replace(slot, moved)


def digest(*parts: str) -> int:
    """A stable 64-bit number from text -- never Python's salted hash()."""
    raw = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
    return int.from_bytes(raw[:8], "big")


def all_elements(root: HtmlElement) -> Iterable[HtmlElement]:
    return (node for node in root.iter() if isinstance(node, HtmlElement))
