"""Every mutation class the schedule may draw from. Tasks 9-11A append their classes here.

Adding a class changes which mutations every future AND past day draws, so a day's DOM is
reproducible from (date, commit), not from the date alone. The seed tag on each stored snapshot
records what was actually applied.
"""

from __future__ import annotations

from testbench.mutations.adversarial.decoy_column import DecoyColumn
from testbench.mutations.adversarial.duplicate_label import DuplicateLabel
from testbench.mutations.adversarial.hidden_decoy import HiddenDecoy
from testbench.mutations.adversarial.image_header import ImageHeader
from testbench.mutations.adversarial.locale_flip import LocaleFlip
from testbench.mutations.adversarial.value_swap import ValueSwap
from testbench.mutations.adversarial.virtualised_rows import VirtualisedRows
from testbench.mutations.base import CatalogueEntry, entry
from testbench.mutations.class_rename import ClassRename
from testbench.mutations.column_reorder import ColumnReorder
from testbench.mutations.dom_restructure import DomRestructure
from testbench.mutations.framework_markup import FrameworkMarkup
from testbench.mutations.id_churn import IdChurn
from testbench.mutations.label_reword import LabelReword
from testbench.mutations.pagination import Pagination
from testbench.mutations.table_to_div import TableToDiv

CATALOGUE: tuple[CatalogueEntry, ...] = (
    *(
        entry(c, adversarial=False)
        for c in (
            ClassRename,
            DomRestructure,
            ColumnReorder,
            TableToDiv,
            LabelReword,
            IdChurn,
            FrameworkMarkup,
            Pagination,
        )
    ),
    *(
        entry(c, adversarial=True)
        for c in (
            DecoyColumn,
            HiddenDecoy,
            DuplicateLabel,
            ValueSwap,
            LocaleFlip,
            VirtualisedRows,
            ImageHeader,
        )
    ),
)
