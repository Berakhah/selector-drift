from __future__ import annotations

from dataclasses import dataclass

from bok_core.provenance.claim import Claim

from selector_drift.resolve.trace import CascadeTrace

type Row = dict[str, Claim]  # type: ignore[type-arg]  # bare Claim, contract §2.1


@dataclass(frozen=True)
class CandidateExtraction:
    rows: list[Row]
    trace: dict[str, CascadeTrace]
    spec_version: int
    run_key: str
