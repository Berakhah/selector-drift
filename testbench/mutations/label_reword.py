"""LabelReword: every column header and detail-page label is reworded to a synonym.

"Claim Number" becomes "Claim No.". Breaks every anchor keyed on exact label text; the meaning
is unchanged, so a resolver that anchors on meaning -- or on the values' format -- heals.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Self

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import elements, grids, header_cells, rows, set_text, text_of
from testbench.mutations.vocabulary import FIELD_ORDER, LABELS, SYNONYMS

DEFAULT_RENAMES: tuple[tuple[str, str], ...] = tuple(
    (LABELS[key], SYNONYMS[key][0]) for key in FIELD_ORDER
)


@dataclass(frozen=True, slots=True)
class LabelReword(MutationBase):
    """`renames` pairs each original label with its new wording; the default is the first
    synonym of every column, so LabelReword() turns "Claim Number" into "Claim No."."""

    renames: tuple[tuple[str, str], ...] = DEFAULT_RENAMES

    def __post_init__(self) -> None:
        old = [o for o, _ in self.renames]
        if not self.renames or len(set(old)) != len(old):
            raise ValueError("renames must name each label once")
        if any(o == n for o, n in self.renames) or {n for _, n in self.renames} & set(old):
            raise ValueError("a new wording must differ from every original label")

    @classmethod
    def from_rng(cls, rng: random.Random) -> Self:
        return cls(renames=tuple((LABELS[key], rng.choice(SYNONYMS[key])) for key in FIELD_ORDER))

    def apply(self, html: str) -> str:
        doc = parse(html)
        mapping = dict(self.renames)
        labels = [cell for grid in grids(doc) for row in rows(grid) for cell in header_cells(row)]
        labels += elements(doc.xpath("//dl/dt"))
        for label in labels:
            new = mapping.get(text_of(label))
            if new is not None:
                set_text(label, new)
                if label.get("aria-label"):
                    label.set("aria-label", new)
        return serialise(doc)
