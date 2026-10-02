"""Escalation to a human: rung 5's only v1 behaviour (design 5.3)."""

from __future__ import annotations

from pathlib import Path

from selector_drift.resolve.trace import CascadeTrace


class EscalateToHuman(Exception):
    """No rung 1-4 resolved. The run quarantines; a human decides. Never a guess.

    `trace` is set by resolve_cascade, so the caller can record the resolution it refused;
    `case` is the escalation-corpus file rung 5 wrote, if it ran.
    """

    def __init__(
        self, reason: str, *, trace: CascadeTrace | None = None, case: Path | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.trace = trace
        self.case = case
