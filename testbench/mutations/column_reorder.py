"""ColumnReorder: the grid's columns are permuted -- headers and every row alike.

Breaks every selector written as td:nth-child(3). Values stay under their own header, so a
resolver that verifies the header heals; one that trusts the index does not.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Self

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import data_cells, data_rows, grids, header_cells, header_row, permute
from testbench.portal.data import COLUMNS

WIDTH = len(COLUMNS)
DEFAULT_ORDER = (0, 2, 3, 1, 5, 4)  # Amount moves from the third column to the second


@dataclass(frozen=True, slots=True)
class ColumnReorder(MutationBase):
    order: tuple[int, ...] = DEFAULT_ORDER

    def __post_init__(self) -> None:
        identity = list(range(len(self.order)))
        if len(self.order) < 2 or sorted(self.order) != identity:
            raise ValueError(f"{self.order} is not a permutation of two or more columns")
        if list(self.order) == identity:
            raise ValueError("the identity permutation is not a reorder")

    @classmethod
    def from_rng(cls, rng: random.Random) -> Self:
        order = list(range(WIDTH))
        while order == list(range(WIDTH)):
            rng.shuffle(order)
        return cls(order=tuple(order))

    def apply(self, html: str) -> str:
        doc = parse(html)
        for grid in grids(doc):
            head = header_row(grid)
            if head is None or len(header_cells(head)) != len(self.order):
                continue  # not a grid this permutation was drawn for
            permute(header_cells(head), self.order)
            for row in data_rows(grid):
                permute(data_cells(row), self.order)
        return serialise(doc)
