"""Every wording a bench column header can carry, and how a mutation finds a column by it.

LabelReword draws its replacements from SYNONYMS. The adversarial classes run after the day's
benign ones, so they find their target column by any of its wordings -- never by position or
class, which ColumnReorder and ClassRename may already have changed.
"""

from __future__ import annotations

from collections.abc import Sequence

from lxml.html import HtmlElement

from testbench.mutations.grid import text_of
from testbench.portal.data import COLUMNS

LABELS: dict[str, str] = dict(COLUMNS)
FIELD_ORDER: tuple[str, ...] = tuple(key for key, _ in COLUMNS)

# No synonym repeats another column's wording or contains another column's label as a phrase
# ("Reserve Amount" would): a benign reword must leave every header naming exactly one column.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "claim_number": ("Claim No.", "Claim #", "Claim Ref.", "Claim ID"),
    "claimant": ("Claimant Name", "Insured Party", "Policyholder"),
    "amount": ("Amount (USD)", "Gross Amount", "Total Amount"),
    "reserve": ("Case Reserve", "Reserve (USD)", "Reserved"),
    "status": ("Current Status", "State", "Stage"),
    "filed": ("Date Filed", "Filed On", "Submitted"),
}


def wordings(key: str) -> frozenset[str]:
    return frozenset((LABELS[key], *SYNONYMS[key]))


def find_column(header: Sequence[HtmlElement], key: str) -> int | None:
    """The index of the header cell naming `key`, by accessible text; None when absent or
    ambiguous (DuplicateLabel's page names claim_number twice)."""
    names = wordings(key)
    hits = [i for i, cell in enumerate(header) if text_of(cell) in names]
    return hits[0] if len(hits) == 1 else None
