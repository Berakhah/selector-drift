"""DecoyColumn: a "Prior Amount" column appears immediately left of Amount.

The adjacent-column attack. Amount shifts one place right, and the position it vacated now
holds plausible amounts, each a little below its row's real one. The decoy is a styled copy of
the Amount column -- same classes, same shape -- so no cached selector still names exactly one
clean Amount cell: a class selector matches two per row, the cached td:nth-child(3) lands on the
decoy. A heal is forced, and a heal by position reads values that pass every format and range
check. This is the mutation the whole validation gate exists to refuse.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Self

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import (
    clone,
    data_cells,
    data_rows,
    digest,
    grids,
    header_cells,
    header_row,
    set_text,
    text_of,
)
from testbench.mutations.vocabulary import find_column
from testbench.portal.data import CENT, MAX_MONEY, MIN_MONEY

DECOY_LABELS = ("Prior Amount", "Previous Amount", "Original Amount")
_SALT_ALPHABET = string.ascii_lowercase + string.digits


@dataclass(frozen=True, slots=True)
class DecoyColumn(MutationBase):
    label: str = "Prior Amount"
    salt: str = "prior"
    low: Decimal = Decimal("0.80")  # decoy / real, drawn per row from [low, high)
    high: Decimal = Decimal("0.97")

    def __post_init__(self) -> None:
        if self.label not in DECOY_LABELS:
            raise ValueError(f"the decoy label must be one of {DECOY_LABELS}")
        if not Decimal("0.5") <= self.low < self.high < Decimal("1"):
            raise ValueError(
                "need 0.5 <= low < high < 1: a decoy close to, and never equal to, the real amount"
            )

    @classmethod
    def from_rng(cls, rng: random.Random) -> Self:
        return cls(label=rng.choice(DECOY_LABELS), salt="".join(rng.choices(_SALT_ALPHABET, k=6)))

    def decoy_value(self, real: str, row: int) -> str:
        """A plausible amount near `real`, never equal to it. Unparseable text is copied."""
        try:
            amount = Decimal(real.replace(",", ""))
        except InvalidOperation:
            return real
        fraction = Decimal(digest(self.salt, str(row), real) % 10_000) / Decimal(10_000)
        ratio = self.low + (self.high - self.low) * fraction
        decoy = min(max((amount * ratio).quantize(CENT), MIN_MONEY), MAX_MONEY)
        if decoy == amount:
            decoy = amount + Decimal("1.00") if amount < MAX_MONEY else amount - Decimal("1.00")
        return f"{decoy:,.2f}"

    def apply(self, html: str) -> str:
        doc = parse(html)
        for grid in grids(doc):
            head = header_row(grid)
            if head is None:
                continue
            header = header_cells(head)
            index = find_column(header, "amount")
            if index is None:
                continue
            twin = clone(header[index], id_suffix="-prior")
            set_text(twin, self.label)
            if twin.get("aria-label"):
                twin.set("aria-label", self.label)
            header[index].addprevious(twin)
            for number, row in enumerate(data_rows(grid)):
                cells = data_cells(row)
                if len(cells) != len(header):
                    continue
                real = cells[index]
                decoy = clone(real, id_suffix="-prior")
                set_text(decoy, self.decoy_value(text_of(real), number))
                real.addprevious(decoy)
        return serialise(doc)
