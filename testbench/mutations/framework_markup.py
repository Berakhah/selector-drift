"""FrameworkMarkup: the page is re-emitted the way a front-end framework would render it.

A portal moving to Vue, Angular or Next wraps its content in a root element, stamps scoped-style
attributes on every node and leaves hydration comments between rows. Nothing a user sees
changes; every structural selector and every attribute-exact selector does.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Self

import lxml.etree
from lxml.html import HtmlElement

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import data_rows, grids, wrap_contents

DIALECTS = ("vue", "angular", "react")


@dataclass(frozen=True, slots=True)
class FrameworkMarkup(MutationBase):
    dialect: str = "vue"
    scope: str = "7a3c9e1f"

    def __post_init__(self) -> None:
        if self.dialect not in DIALECTS:
            raise ValueError(f"unknown dialect {self.dialect!r}")
        if len(self.scope) != 8 or any(c not in "0123456789abcdef" for c in self.scope):
            raise ValueError("scope must be eight lower-case hex digits")

    @classmethod
    def from_rng(cls, rng: random.Random) -> Self:
        return cls(dialect=rng.choice(DIALECTS), scope=f"{rng.getrandbits(32):08x}")

    def _root(self) -> tuple[str, dict[str, str]]:
        if self.dialect == "vue":
            return "div", {"id": "app", "data-v-app": ""}
        if self.dialect == "angular":
            return "app-root", {"ng-version": "17.3.12", f"_nghost-ng-c{self.scope}": ""}
        return "div", {"id": "__next"}

    def _scoped_attribute(self) -> str | None:
        if self.dialect == "vue":
            return f"data-v-{self.scope}"
        if self.dialect == "angular":
            return f"_ngcontent-ng-c{self.scope}"
        return None  # React stamps no per-node attribute

    def _fragment_markers(self) -> tuple[str, str] | None:
        if self.dialect == "vue":
            return "[", "]"  # Vue SSR fragment
        if self.dialect == "react":
            return "$", "/$"  # React Suspense boundary
        return None

    def apply(self, html: str) -> str:
        doc = parse(html)
        container = doc.find(".//main")
        if container is None:
            container = doc.find("body")
        if container is None:
            return serialise(doc)
        tag, attrs = self._root()
        root = wrap_contents(container, tag, attrs)
        scoped = self._scoped_attribute()
        if scoped is not None:
            for node in root.iterdescendants():
                if isinstance(node, HtmlElement):
                    node.set(scoped, "")
        markers = self._fragment_markers()
        if markers is not None:
            for grid in grids(root):
                body = data_rows(grid)
                if body:
                    body[0].addprevious(lxml.etree.Comment(markers[0]))
                    body[-1].addnext(lxml.etree.Comment(markers[1]))
        return serialise(doc)
