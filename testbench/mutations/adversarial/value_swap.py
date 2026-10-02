"""ValueSwap: every row's Amount and Reserve values change places. The markup does not move.

The attack on everything structural and everything textual at once: both columns keep their
headers, cells, classes and paths, both hold plausible amounts, and no footing rule relates them
(Task 7). A resolver heals to exactly the right node and reads the wrong number. Only
RowKeyContinuity (Task 22) -- the same key, a different value, on nearly every row -- can see it.
"""

from __future__ import annotations

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import (
    data_cells,
    data_rows,
    grids,
    header_cells,
    header_row,
    set_text,
    text_of,
)
from testbench.mutations.vocabulary import find_column


class ValueSwap(MutationBase):
    def apply(self, html: str) -> str:
        doc = parse(html)
        for grid in grids(doc):
            head = header_row(grid)
            if head is None:
                continue
            header = header_cells(head)
            amount, reserve = find_column(header, "amount"), find_column(header, "reserve")
            if amount is None or reserve is None:
                continue
            for row in data_rows(grid):
                cells = data_cells(row)
                if len(cells) != len(header):
                    continue
                a, r = text_of(cells[amount]), text_of(cells[reserve])
                set_text(cells[amount], r)
                set_text(cells[reserve], a)
        return serialise(doc)
