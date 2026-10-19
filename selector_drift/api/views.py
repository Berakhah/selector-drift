"""Every request and response model of the API (API spec 4.3). The only module the type
generator reads (scripts/gen-types.sh, Task 34C).

Rule 1: no field without a column (or derive.py) behind it.
Rule 2: every extracted value is a rendered Claim (`rail`), never a bare string.
Rule 3: every check result is a Verdict; no boolean here means "passed".
Verdict, Family, AnchorEvidence, Basis, FirstHealPolicy are imported from their owning
packages, never redeclared, so a change to them is a change to this contract.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from bok_core.boundary.sensitivity import Sensitivity
from bok_core.findings import Severity
from bok_core.provenance.render import RailPayload
from pydantic import BaseModel, ConfigDict, Field

from selector_drift.heallog.capture import BBox
from selector_drift.heallog.diff import DiffSpan
from selector_drift.resolve.trace import AnchorEvidence, Family
from selector_drift.scope.record import Basis, FirstHealPolicy
from selector_drift.validate.verdict import Verdict

__all__ = [
    "AttemptView",
    "BBox",
    "DecisionRequest",
    "DecisionView",
    "DiffSpan",
    "EvidenceSideView",
    "ExtractedValueView",
    "FindingView",
    "HealView",
    "LoginRequest",
    "OperatorView",
    "Page",
    "QuarantineView",
    "RateLimitView",
    "RequestModel",
    "ResolutionView",
    "RunDetailView",
    "RunView",
    "RungAttemptView",
    "SpecView",
    "StatsView",
    "TargetView",
    "ViewModel",
]


class RequestModel(BaseModel):
    """Every request body. extra="forbid": a body carrying `actor` is a 422 (H3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ViewModel(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------- requests


class LoginRequest(RequestModel):
    username: str = Field(pattern=r"^[a-z0-9_.-]{1,64}$")
    password: str = Field(min_length=1, max_length=1024)


class DecisionRequest(RequestModel):
    reason: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------- responses


class OperatorView(ViewModel):
    username: str


class RateLimitView(ViewModel):
    requests: int
    per_seconds: int


class TargetView(ViewModel):
    target: str
    status: Literal["active", "expired", "unscoped"]
    locked_reason: str | None  # set iff status != "active"; words, never a code
    basis: Basis | None  # None iff unscoped
    sensitivity: Sensitivity | None  # DERIVED from basis (R4) -- displayed, never editable
    authorising_party: str | None
    authorised_on: date | None
    expires_on: date | None
    evidence_uri: str | None
    evidence_sha256: str | None
    origins: list[str]
    rate_limit: RateLimitView | None
    user_agent_contact: str | None
    first_heal_policy: FirstHealPolicy | None
    warmup_runs: int | None
    evidence_retention_days: int | None  # None = kept for the life of the log (self-owned only)
    legal_review: str | None
    record_sha256: str | None
    loaded_at: datetime | None


class RunView(ViewModel):
    run_key: str
    target: str
    spec_version: int
    window: date
    state: Literal["completed", "healed", "quarantined", "failed", "refused"]  # the frozen five
    quarantine_resolution: Literal["promoted", "discarded"] | None
    attempts: int
    first_started_at: datetime
    last_finished_at: datetime | None
    rows_written: int | None
    error: str | None


class AttemptView(ViewModel):
    attempt: int
    started_at: datetime
    finished_at: datetime | None
    state: str
    error: str | None
    scope_record_sha256: str


class RungAttemptView(ViewModel):
    rung: int
    family: Family
    outcome: Literal[
        "resolved", "no_match", "ambiguous", "not_visible", "anchor_mismatch", "escalated"
    ]
    ms: int


class ResolutionView(ViewModel):
    field: str
    winning_rung: int | None  # None: no rung resolved
    attempts: list[RungAttemptView]
    node_id: str | None
    heal_id: int | None


class RunDetailView(RunView):
    run_attempts: list[AttemptView]
    resolutions: list[ResolutionView]
    finding_count: int


class DecisionView(ViewModel):
    # Derived, not stored as one column:
    #   "accepted"  -- the gate passed with no human (runs.state == healed, no quarantine
    #                  row); includes a first heal passed by first_heal_policy: auto_promote
    #   "promoted"  -- quarantine.resolved_as == promoted (heal_events.promoted_by is the actor)
    #   "discarded" -- quarantine.resolved_as == discarded
    #   None        -- still quarantined, awaiting a decision
    resolved_as: Literal["accepted", "promoted", "discarded"] | None
    actor: str | None  # None for "accepted": no human decided
    at: datetime | None
    reason: str | None


class EvidenceSideView(ViewModel):
    available: bool
    unavailable_reason: Literal["corrupted", "expired", "not_captured"] | None
    captured_at: datetime | None
    viewport: tuple[int, int] | None
    boxes: dict[str, BBox]  # field -> box recorded at capture (API spec 7.2)
    dom_url: str | None  # /api/v1/heals/{id}/evidence/{side}/dom
    png_url: str | None


class ExtractedValueView(ViewModel):
    field: str
    row_key: dict[str, str]  # natural-key values identifying the row
    value: str
    rail: RailPayload  # bok_core.provenance.render.to_rail(claim) -- DomSource


class HealView(ViewModel):
    id: int
    run_key: str
    target: str
    field: str
    broke_at: datetime
    winning_rung: int
    confirmer_rung: int | None  # None when the confirmer was the VALUE family
    confirmer_family: Family | None  # None: no cross-family confirmer resolved
    selector_before: str | None
    selector_after: str
    anchor_before: AnchorEvidence | None
    anchor_after: AnchorEvidence
    trace: list[RungAttemptView]
    verdicts: list[Verdict]
    replay_status: Literal["reproduced", "not_reproducible", "not_yet_replayed", "corrupted"]
    before: EvidenceSideView
    after: EvidenceSideView
    sample_values: list[ExtractedValueView]  # first 10 rows of the healed field
    decision: DecisionView
    summary: str  # the design 5.4 sentence, built in derive.py


class QuarantineView(ViewModel):
    run_key: str
    target: str
    reason: str
    verdicts: list[Verdict]
    failing: list[Verdict]  # outcome FAIL -- INSUFFICIENT is tolerated once a
    # human confirms (design 5.9), so it is not listed
    preview: list[dict[str, ExtractedValueView]]  # first 20 rows
    heal_ids: list[int]
    spec_excerpt: str  # the FieldTarget YAML for each failing field (M11)
    runbook_url: str  # docs/runbook-spec-update.md
    decision: DecisionView


class FindingView(ViewModel):  # bok_core Finding, flattened for display
    id: str
    run_key: str
    severity: Severity
    title: str
    check: str  # properties["rule"]
    observed: str
    threshold: str
    threshold_reason: str  # required -- brief 8 screen 5
    rail: RailPayload


class SpecView(ViewModel):
    target: str
    version: int
    yaml: str
    validation: list[Verdict]


class StatsView(ViewModel):  # API spec 6.2 -- plain figures with a method link
    window: tuple[date, date]
    elapsed_days: int
    scheduled_runs: int
    missed_runs: int
    fired_without_attempt: int
    run_success_rate: float  # completed+healed / attempted, from run_attempts
    rung_shares: dict[str, float]  # rung1..rung5, from resolutions; {} before the first
    heals: int
    quarantines: int
    quarantines_refused_correctly: int | None  # bench only; None without an answer key
    # bench only; None without an answer key — a wrong heal the gate let through
    false_accepts: int | None
    manual_interventions: int  # manual SELECTOR fixes: spec edits (not schedule) and
    # cache edits -- the "zero manual fixes" headline
    human_confirmations: int  # manual_promote rows: first-heal confirmations, a
    # designed step, never counted as a fix (2026-09-26)
    method_url: str  # docs/methodology.md anchor for these figures


class Page[T](ViewModel):
    items: list[T]
    next_cursor: str | None  # opaque; base64 of (first_started_at, run_key)
