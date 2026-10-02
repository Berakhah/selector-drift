"""DomRestructure: the grid is re-nested and every cell's content is wrapped.

A redesign that moves the table into a card, promotes its caption to a heading and wraps each
cell's text in a span for styling. Every absolute path to a cell changes; no value does.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Self

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import (
    data_cells,
    detach,
    grids,
    header_cells,
    rows,
    wrap,
    wrap_contents,
)

WRAPPER_POOL = (
    "table-responsive",
    "card",
    "card-body",
    "panel",
    "panel-body",
    "grid-shell",
    "content-block",
)
INNER_TAGS = ("span", "div")


@dataclass(frozen=True, slots=True)
class DomRestructure(MutationBase):
    wrappers: tuple[str, ...] = ("table-responsive", "card-body")
    inner_tag: str = "span"

    def __post_init__(self) -> None:
        if not 1 <= len(self.wrappers) <= 3:
            raise ValueError("one to three wrappers")
        if self.inner_tag not in INNER_TAGS:
            raise ValueError(f"inner_tag must be one of {INNER_TAGS}")

    @classmethod
    def from_rng(cls, rng: random.Random) -> Self:
        depth = rng.randint(1, 3)
        return cls(
            wrappers=tuple(rng.sample(WRAPPER_POOL, depth)), inner_tag=rng.choice(INNER_TAGS)
        )

    def apply(self, html: str) -> str:
        doc = parse(html)
        for grid in grids(doc):
            outer = grid
            for cls in reversed(self.wrappers):
                outer = wrap(outer, "div", {"class": cls})
            caption = grid.find("caption")
            if caption is not None:
                heading = grid.makeelement("h2", {**dict(caption.attrib), "class": "grid-title"})
                heading.text = caption.text
                for child in list(caption):
                    heading.append(child)
                grid.addprevious(heading)
                caption.text = None
                detach(caption)
            for row in rows(grid):
                for cell in (*header_cells(row), *data_cells(row)):
                    wrap_contents(cell, self.inner_tag, {"class": "cell-content"})
        return serialise(doc)
