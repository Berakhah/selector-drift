"""The scope record: the only place a target's legal and technical boundaries are declared.

`ScopeRecord` is author-facing (one per target, hand-written or reviewed as YAML in Task 5).
`AuthorisedTarget` is the gate-facing result of `scope.gate.authorise()` -- the only object every
later stage (browser driver, resolver, orchestrator) is allowed to hold. Nothing outside
`scope.gate` may construct one; see `_token.py` and `.importlinter`'s `only-the-gate-mints`
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from bok_core.boundary.sensitivity import Sensitivity
from pydantic import BaseModel, Field

from selector_drift.scope._token import _MINT, _ScopeToken
from selector_drift.scope.errors import ScopeRefusal


class Basis(StrEnum):
    """The only two authorisation bases v1 supports (see 00 R2 / brief design 4.2).
    Deliberately not a free-text field: an unrecognised basis fails to parse rather than
    silently falling through to some default posture.
    """

    SELF_OWNED = "self-owned"
    CLIENT_AUTHORISED = "client-authorised"


class FirstHealPolicy(StrEnum):
    REQUIRED = "required"
    AUTO_PROMOTE = "auto_promote"


@dataclass(frozen=True)
class RateLimit:
    requests: int
    per_seconds: int


class ScopeRecord(BaseModel):
    """Author-facing. One per target. `sensitivity` is deliberately NOT a field here --
    see the derived property below (R4/H7)."""

    target: str
    basis: Basis
    authorising_party: str
    authorised_on: date
    expires_on: date
    evidence_uri: str | None
    evidence_sha256: str | None
    origins: list[str]
    rate_limit: RateLimit
    user_agent_contact: str
    first_heal_policy: FirstHealPolicy
    legal_review: str
    # Both join first_heal_policy for the same reason: a value a client target must never
    # quietly lower lives on the scope record, next to the authorisation basis.
    warmup_runs: int = Field(default=30, ge=1)
    evidence_retention_days: int | None = Field(default=None, ge=1)

    @property
    def sensitivity(self) -> Sensitivity:
        """Derived from `basis`, never authored. A free-text label that gates
        publication means one typo in one YAML file publishes a client's portal
        screenshots -- the exact outcome B4 exists to prevent, reachable with no
        second control. Derived, it cannot be typed wrong.

        A plain @property, not @computed_field: a computed field enters
        `model_dump()`, which would change what `record_sha256` hashes.
        """
        if self.basis is Basis.SELF_OWNED:
            return Sensitivity.SYNTHETIC
        return Sensitivity.CLIENT_CONFIDENTIAL


@dataclass(frozen=True)
class AuthorisedTarget:
    """Gate-facing. The only object `scope.gate.authorise()` returns and the only object
    every later stage may hold. Construction requires the singleton `_MINT` token; see
    `_token.py`. `__post_init__` checks *identity*, not `isinstance`, against `_MINT` (M29):
    a leading underscore on `_ScopeToken` is a convention, not a control, so someone WILL
    import it directly -- only identity against the one instance `gate.py` mints catches
    that.
    """

    _token: _ScopeToken
    target: str
    basis: Basis
    rate_limit: RateLimit
    origins: tuple[str, ...]  # H2 -- the browser allowlist
    user_agent_contact: str
    first_heal_policy: FirstHealPolicy
    sensitivity: Sensitivity  # derived from basis, never authored
    expires_on: date
    record_sha256: str  # H10 -- what this run will write to audit_log
    warmup_runs: int = 30
    evidence_retention_days: int | None = None

    def __post_init__(self) -> None:
        if self._token is not _MINT:  # identity, not isinstance (M29)
            raise ScopeRefusal("not minted by the scope gate")
