"""HiddenDecoy: an invisible grid of well-formed claim numbers, labelled "Claim Number", placed
ahead of the real table.

The rung-2 attack (H9). Every decoy cell carries aria-label="Claim Number" and a value that
passes the claim-number format assertion, and it comes first in document order. It is hidden
either from assistive technology and the viewport (aria-hidden plus off-screen) or from
rendering altogether (display:none), so only a resolver that filters candidates through
visible() -- Task 13 -- refuses it. Both variants carry aria-hidden="true" (2026-09-26): a real
portal's hidden cache is hidden from assistive technology too, and it keeps the attack decidable
from markup, so the static and live page sources judge it identically (Task 13's parity rule).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Self

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import data_cells, data_rows, grids, header_cells, header_row, text_of
from testbench.mutations.vocabulary import LABELS, find_column

VARIANTS = ("aria-hidden", "display-none")
CLAIM_NUMBER = re.compile(r"([A-Z]{2})(\d{8})")
DECOY_LABEL = LABELS["claim_number"]  # the clean run's anchor text, whatever today's is
_CONTAINER: dict[str, dict[str, str]] = {
    "aria-hidden": {
        "class": "sr-cache",
        "aria-hidden": "true",
        "style": "position:absolute;left:-10000px;width:1px;height:1px;overflow:hidden",
    },
    "display-none": {"class": "sr-cache", "aria-hidden": "true", "style": "display:none"},
}


@dataclass(frozen=True, slots=True)
class HiddenDecoy(MutationBase):
    variant: str = "aria-hidden"
    offset: int = 7_340_021  # added to each real number's digits, modulo 10**8

    def __post_init__(self) -> None:
        if self.variant not in VARIANTS:
            raise ValueError(f"unknown variant {self.variant!r}")
        if not 0 < self.offset < 10**8:
            raise ValueError("offset must be in (0, 10**8) so no decoy equals its real number")

    @classmethod
    def from_rng(cls, rng: random.Random) -> Self:
        return cls(variant=rng.choice(VARIANTS), offset=rng.randrange(1, 10**8))

    def decoy_number(self, real: str) -> str | None:
        match = CLAIM_NUMBER.fullmatch(real)
        if match is None:
            return None
        return f"{match.group(1)}{(int(match.group(2)) + self.offset) % 10**8:08d}"

    def apply(self, html: str) -> str:
        doc = parse(html)
        for grid in grids(doc):  # listed before any decoy grid exists
            head = header_row(grid)
            if head is None:
                continue
            header = header_cells(head)
            index = find_column(header, "claim_number")
            if index is None:
                continue
            numbers = [
                self.decoy_number(text_of(cells[index]))
                for row in data_rows(grid)
                if len(cells := data_cells(row)) == len(header)
            ]
            container = grid.makeelement("div", dict(_CONTAINER[self.variant]))
            decoy_grid = container.makeelement("div", {"role": "grid", "aria-label": "Claims"})
            container.append(decoy_grid)
            for number in numbers:
                if number is None:
                    continue
                row = decoy_grid.makeelement("div", {"role": "row"})
                cell = row.makeelement("span", {"role": "gridcell", "aria-label": DECOY_LABEL})
                cell.text = number
                row.append(cell)
                decoy_grid.append(row)
            grid.addprevious(container)
        return serialise(doc)
