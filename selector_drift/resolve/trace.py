"""The resolver's vocabulary: what a rung returns, what the cascade records.

Lives here, not in resolve/rungs/, because validate/ must reach it and the
judge-reaches-rungs-only-by-protocol contract forbids validate -> resolve.rungs; the API's
api-is-read-side contract permits api -> resolve.trace and nothing else under resolve/.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from selector_drift.resolve.page import PageSource
from selector_drift.spec.schema import FieldTarget


class Family(StrEnum):
    """Signal family. The value is what resolutions.attempts and
    heal_events.confirmer_family store, so it is the name, upper-case."""

    TEXT_SEMANTIC = "TEXT_SEMANTIC"
    STRUCTURAL = "STRUCTURAL"
    VALUE = "VALUE"
    NONE = "NONE"


class AnchorEvidence(BaseModel):
    """What justified a selector at heal time (design 5.2). Rung 1 re-verifies it every run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accessible_name: str | None = None  # rung 2
    label_text: str | None = None  # rung 3
    header_text: str | None = None  # rung 4

    def is_empty(self) -> bool:
        return self.accessible_name is None and self.label_text is None and self.header_text is None


@dataclass(frozen=True)
class ResolvedElement:
    selector: str
    node_id: str  # minted by the PageSource, never derived from the selector (C5)
    anchor: AnchorEvidence
    rung_id: int


AttemptOutcome = Literal[
    "resolved", "no_match", "ambiguous", "not_visible", "anchor_mismatch", "escalated"
]
MissOutcome = Literal["no_match", "ambiguous", "not_visible", "anchor_mismatch"]


@dataclass(frozen=True)
class Miss:
    """Why a rung declined. The cascade records `outcome`; `detail` is for humans and logs."""

    outcome: MissOutcome
    detail: str


@dataclass(frozen=True)
class RungAttempt:
    rung: int
    family: Family
    outcome: AttemptOutcome
    ms: int

    def as_json(self) -> dict[str, object]:  # exactly what resolutions.attempts stores
        return {
            "rung": self.rung,
            "family": self.family.value,
            "outcome": self.outcome,
            "ms": self.ms,
        }


@dataclass(frozen=True)
class CascadeTrace:
    attempts: tuple[RungAttempt, ...]
    winner: int | None
    resolution_id: int | None = None  # the resolutions row written; None on replay

    @property
    def healed(self) -> bool:
        """True unless rung 1 won. Task 20's asymmetry reads exactly this."""
        return self.winner != 1

    def as_json(self) -> list[dict[str, object]]:
        return [a.as_json() for a in self.attempts]


@runtime_checkable
class Rung(Protocol):
    """The only thing validate/ may know about a rung (Task 1 contract)."""

    @property
    def rung_id(self) -> int: ...

    @property
    def family(self) -> Family: ...

    def resolve(self, page: PageSource, target: FieldTarget) -> ResolvedElement | None: ...


@runtime_checkable
class DiagnosableRung(Rung, Protocol):
    """A rung that can say why it declined. The cascade prefers this to resolve()."""

    def attempt(self, page: PageSource, target: FieldTarget) -> ResolvedElement | Miss: ...
