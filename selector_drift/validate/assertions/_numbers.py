"""One strict number parser for every check that reads an amount."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Final

# 1,234.56 or 1234.56 or -12. ASCII digits only: a bare \d also matches Arabic-Indic digits that
# Decimal would read. Linear-time: no nested quantifier can backtrack.
_NUMBER: Final = re.compile(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", re.ASCII)


def parse_number(text: str) -> Decimal | None:
    s = text.strip()
    if len(s) > 64 or not _NUMBER.fullmatch(s):
        return None  # "1.234,56" (LocaleFlip) is deliberately not a number
    return Decimal(s.replace(",", ""))
