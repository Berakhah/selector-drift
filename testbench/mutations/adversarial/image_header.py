"""ImageHeader: the Amount header is rendered as an image of its label.

<th><img alt="Amount"></th>: a sighted user and a screen reader both read "Amount"; the header's
textContent is empty. Rung 4's header verification must read accessible text, not textContent,
or it cannot confirm the column -- and a resolver that falls back to the nearest header with
text lands on the neighbour.
"""

from __future__ import annotations

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import grids, header_cells, header_row, innermost, text_of
from testbench.mutations.vocabulary import find_column


class ImageHeader(MutationBase):
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
            label = text_of(header[index])
            holder = innermost(header[index])
            for child in list(holder):
                holder.remove(child)
            holder.text = None
            holder.append(holder.makeelement("img", {"alt": label}))
        return serialise(doc)
