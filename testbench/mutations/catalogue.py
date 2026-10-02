"""Every mutation class the schedule may draw from. Tasks 9-11A append their classes here.

Adding a class changes which mutations every future AND past day draws, so a day's DOM is
reproducible from (date, commit), not from the date alone. The seed tag on each stored snapshot
records what was actually applied.
"""

from __future__ import annotations

from testbench.mutations.base import CatalogueEntry

CATALOGUE: tuple[CatalogueEntry, ...] = ()
