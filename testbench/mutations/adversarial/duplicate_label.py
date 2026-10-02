"""DuplicateLabel: a second column with the claim-number header appears before the real one.

The portal starts showing a legacy claim reference beside the new one, under the same label and
in the same format. Rung 1's "exactly one element" and rung 3's label anchor both see two
matches; a resolver that takes the first match reads the legacy numbers, every one of which
passes the format assertion.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Self

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import (
    clone,
    data_cells,
    data_rows,
    grids,
    header_cells,
    header_row,
    set_text,
    text_of,
)
from testbench.mutations.vocabulary import find_column

CLAIM_NUMBER = re.compile(r"([A-Z]{2})(\d{8})")


@dataclass(frozen=True, slots=True)
class DuplicateLabel(MutationBase):
    shift: int = 3  # letters rotated by this much; digits reversed

    def __post_init__(self) -> None:
        if not 0 < self.shift < 26:
            raise ValueError("shift must be in (0, 26) so no legacy number equals its real one")

    @classmethod
    def from_rng(cls, rng: random.Random) -> Self:
        return cls(shift=rng.randrange(1, 26))

    def legacy_number(self, real: str) -> str:
        match = CLAIM_NUMBER.fullmatch(real)
        if match is None:
            return real
        letters = "".join(
            chr((ord(c) - ord("A") + self.shift) % 26 + ord("A")) for c in match.group(1)
        )
        return letters + match.group(2)[::-1]

    def apply(self, html: str) -> str:
        doc = parse(html)
        for grid in grids(doc):
            head = header_row(grid)
            if head is None:
                continue
            header = header_cells(head)
            index = find_column(header, "claim_number")
            if index is None:
                continue
            header[index].addprevious(clone(header[index], id_suffix="-legacy"))
            for row in data_rows(grid):
                cells = data_cells(row)
                if len(cells) != len(header):
                    continue
                real = cells[index]
                legacy = clone(real, id_suffix="-legacy")
                set_text(legacy, self.legacy_number(text_of(real)))
                real.addprevious(legacy)
        return serialise(doc)
