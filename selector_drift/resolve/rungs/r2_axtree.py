"""Rung 2: role + accessible name + relationship, read from the accessibility tree."""

from __future__ import annotations

from lxml.html import HtmlElement

from selector_drift.resolve.grid import cell_right_of, cell_under
from selector_drift.resolve.labels import expected_label, normalise
from selector_drift.resolve.page import PageSource
from selector_drift.resolve.rungs.base import (
    AnchorEvidence,
    Family,
    Miss,
    ResolvedElement,
    RungBase,
    unique,
)
from selector_drift.spec.schema import FieldTarget

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


def locate_by_name(page: PageSource, name: str) -> tuple[HtmlElement, str] | Miss:
    """The one visible element that `name` labels, and the name as the page spells it.
    Also rung 1's re-verification of a stored accessible_name (Task 17)."""
    want = normalise(name)
    hits: list[tuple[HtmlElement, str]] = []
    saw_hidden = False
    for node in page.ax().walk():
        if not want or normalise(node.name) != want:
            continue
        anchor = page.node(node.node_id)
        if anchor is None:
            continue
        if not page.visible(anchor):
            saw_hidden = True  # H9: a hidden labelled node is never a hit
            continue
        if node.role == "columnheader":
            value = cell_under(page, anchor)
        elif node.role == "rowheader":
            value = cell_right_of(page, anchor)
        elif node.role in VALUE_ROLES:
            value = anchor
        else:
            continue
        if value is None:
            continue
        if not page.visible(value):
            saw_hidden = True
            continue
        hits.append((value, node.name))
    values = unique([v for v, _ in hits])
    if not values:
        return Miss(
            "not_visible" if saw_hidden else "no_match", f"no visible element is named {name!r}"
        )
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
