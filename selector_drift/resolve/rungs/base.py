"""Shared by the concrete rungs. Re-exports the Rung protocol from resolve.trace."""

from __future__ import annotations

from lxml.html import HtmlElement

from selector_drift.resolve.aria import element_children, parent_of, tag_of
from selector_drift.resolve.page import PageSource
from selector_drift.resolve.trace import (
    AnchorEvidence,
    DiagnosableRung,
    Family,
    Miss,
    ResolvedElement,
    Rung,
)
from selector_drift.spec.schema import FieldTarget

__all__ = [
    "AnchorEvidence",
    "DiagnosableRung",
    "Family",
    "Miss",
    "ResolvedElement",
    "Rung",
    "RungBase",
    "css_path",
    "text_of",
    "unique",
]


def text_of(el: HtmlElement) -> str:
    return " ".join(el.text_content().split())


def css_path(el: HtmlElement) -> str:
    """An absolute, structural CSS path: exactly one element by construction. Deliberately
    not an id or class selector -- IdChurn and ClassRename then break rung 1 and a heal is
    recorded, rather than a restyled page silently matching something else."""
    steps: list[str] = []
    node: HtmlElement | None = el
    while node is not None:
        parent = parent_of(node)
        tag = tag_of(node)
        if parent is None:
            steps.append(tag)
        else:
            same = [c for c in element_children(parent) if tag_of(c) == tag]
            steps.append(f"{tag}:nth-of-type({same.index(node) + 1})")
        node = parent
    return " > ".join(reversed(steps))


def unique(found: list[HtmlElement]) -> list[HtmlElement]:
    out: list[HtmlElement] = []
    for el in found:
        if not any(el is o for o in out):
            out.append(el)
    return out


class RungBase:
    """resolve() is attempt() with the reason dropped -- the Rung protocol validate/ uses."""

    rung_id: int = 0
    family: Family = Family.NONE

    def attempt(self, page: PageSource, target: FieldTarget) -> ResolvedElement | Miss:
        raise NotImplementedError

    def resolve(self, page: PageSource, target: FieldTarget) -> ResolvedElement | None:
        result = self.attempt(page, target)
        return result if isinstance(result, ResolvedElement) else None

    def found(self, page: PageSource, el: HtmlElement, anchor: AnchorEvidence) -> ResolvedElement:
        return ResolvedElement(
            selector=css_path(el), node_id=page.node_id(el), anchor=anchor, rung_id=self.rung_id
        )
