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
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
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
from testbench.portal.data import CENT, MIN_MONEY

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
        """A plausible amount 80-97% of `real`, never equal to it. Unparseable text is copied.

        Clamping the dollar value to MIN_MONEY (as a first draft did) can push the *ratio* above
        `self.high`, or even above 1, for `real` near MIN_MONEY -- a decoy greater than the real
        amount. Clamp the ratio instead: raise the effective floor to whatever ratio keeps the
        result at or above MIN_MONEY, and only fall back to MIN_MONEY itself when no ratio in
        [low, high] can reach it. At real == MIN_MONEY exactly there is no plausible value
        strictly below it, so the decoy is MIN_MONEY + one cent -- the single documented
        out-of-band case, because plausibility outranks the band.
        """
        try:
            amount = Decimal(real.replace(",", ""))
        except InvalidOperation:
            return real
        if amount <= MIN_MONEY:
            decoy = MIN_MONEY + CENT if amount == MIN_MONEY else MIN_MONEY
            return f"{decoy:,.2f}"
        low_eff = max(self.low, MIN_MONEY / amount)
        if low_eff > self.high:
            return f"{MIN_MONEY:,.2f}"  # no ratio in [low, high] keeps the result >= MIN_MONEY
        fraction = Decimal(digest(self.salt, str(row), real) % 10_000) / Decimal(10_000)
        ratio = low_eff + (self.high - low_eff) * fraction
        decoy = (amount * ratio).quantize(CENT)
        # Rounding at the quantize step can tip the result just outside the band or under
        # MIN_MONEY; nudge it back to the nearest interior cent.
        floor = max(MIN_MONEY, (amount * low_eff).quantize(CENT, rounding=ROUND_CEILING))
        ceiling = min((amount * self.high).quantize(CENT, rounding=ROUND_FLOOR), amount - CENT)
        if floor > ceiling:  # low_eff and high rounded to the same cent at this boundary
            floor = ceiling
        decoy = min(max(decoy, floor), ceiling)
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
