"""A check's answer. Never a bare boolean: the UI has nothing else it could render (design 5.5)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from selector_drift.resolve.trace import Family


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"


class Confirmation(BaseModel):
    """Which independent signal confirmed a heal. `rung` is None for the VALUE family."""

    model_config = ConfigDict(frozen=True)

    rung: int | None
    family: Family


class Verdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str = Field(min_length=1)
    outcome: Outcome
    threshold: str | None
    observed: str | None
    reason: str = Field(min_length=1)
    field: str | None = None  # stamped by gate_run
    # Set ONLY by IndependentRungAgreement and RowKeyContinuity, and only on PASS.
    confirmed_by: Confirmation | None = None

    @model_validator(mode="after")
    def _only_a_pass_names_a_confirmer(self) -> Verdict:
        if self.confirmed_by is not None and self.outcome is not Outcome.PASS:
            raise ValueError("only a PASS can name a confirmer")
        return self
