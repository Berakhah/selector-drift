"""The one day-one placeholder gate (fix round 1, Important 1).

`tests/scope/test_records_load.py` and `tests/spec/test_loader.py` both need to know whether the
day-one scope records (`scopes/*.yaml`) still carry an unresolved `<UPPER-CASE>` placeholder or
the literal token `USER`, so each can skip the tests that must load the real records until
day-one values (P-2/P-3/P-4) land. Defined once here and imported by both -- previously each
file defined its own copy of `PLACEHOLDER`, and they had already diverged (one carried the
`\\bUSER\\b` branch, the other did not), so a value left as literal `USER` after every
`<UPPER-CASE>` token was filled would have made the two halves of the gate lift inconsistently:
one file's tests would start running while the other's stayed skipped, against the same scope
records.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCOPES = sorted((REPO / "scopes").glob("*.yaml"))
PLACEHOLDER = re.compile(r"<[A-Z0-9][A-Z0-9-]*>|\bUSER\b")


def _any_scope_has_a_placeholder() -> bool:
    return any(PLACEHOLDER.search(p.read_text(encoding="utf-8")) for p in SCOPES)


blocked_on_day_one_values = pytest.mark.skipif(
    _any_scope_has_a_placeholder(),
    reason="blocked on day-one values (P-2/P-3/P-4) -- "
    "see test_no_scope_record_carries_an_unresolved_placeholder",
)
