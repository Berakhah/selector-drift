"""LocaleFlip: every currency value switches to German grouping -- 1,234.56 becomes 1.234,56.

The portal's locale setting changes. The values are the same numbers, written so that a loose
numeric pattern reads 1.234,56 as 1.234 and 193,45 as 19,345. The DomainAssertion's range
check and brief §5's currency-sanity rule must catch it; the format assertion alone may not.
"""

from __future__ import annotations

import re

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import data_cells, data_rows, elements, grids, set_text, text_of

CURRENCY = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}")


def flip(value: str) -> str:
    """1,234.56 -> 1.234,56. A value already flipped no longer matches CURRENCY."""
    return value.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


class LocaleFlip(MutationBase):
    def apply(self, html: str) -> str:
        doc = parse(html)
        cells = [c for grid in grids(doc) for row in data_rows(grid) for c in data_cells(row)]
        for cell in (*cells, *elements(doc.xpath("//dl/dd"))):
            text = text_of(cell)
            if CURRENCY.fullmatch(text):
                set_text(cell, flip(text))
        return serialise(doc)
