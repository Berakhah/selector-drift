"""The one exception the scope gate raises. Fails closed: every refusal path in
`gate.py` and `record.py` raises this, never returns a falsy value or logs and continues.
"""

from __future__ import annotations


class ScopeRefusal(Exception):
    """Raised whenever a target may not be authorised. `.reason` is the human-readable
    refusal, also carried as the exception's message so `pytest.raises(..., match=...)`
    and any caller that just does `str(exc)` both see the same text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
