"""The scope record: the only place a target's legal and technical boundaries are declared.

`ScopeRecord` is author-facing (one per target, hand-written or reviewed as YAML in Task 5).
`AuthorisedTarget` is the gate-facing result of `scope.gate.authorise()` -- the only object every
later stage (browser driver, resolver, orchestrator) is allowed to hold. Nothing outside
`scope.gate` may construct one; see `_token.py` and `.importlinter`'s `only-the-gate-mints`
contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from urllib.parse import urlsplit

from bok_core.boundary.sensitivity import Sensitivity
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Aliased (not `from ... import _MINT`), so this module's own namespace never re-exports the
# mint token or the minting flag -- see fix round 1, I-2. Aliased to a name distinct from the
# `_token` dataclass field below, to keep that field's forward-referenced annotation
# (`_token_module._ScopeToken`) unambiguous for a static type checker.
from selector_drift.scope import _token as _token_module
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.origin import parse_origin


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


_TARGET_SLUG = re.compile(r"^[a-z0-9-]{1,64}$")
# No whitespace, angle brackets, or other characters a browser would reject or mis-parse in a
# URL -- deliberately excludes '<' and '>' so an unresolved `<REAL-ACCOUNT>`-style placeholder
# (ruling 3, deferred Task 2 finding) fails here rather than loading as a plausible URL.
_URL_UNSAFE_CHARS = re.compile(r"[\s<>\"^`{|}\\]")


@dataclass(frozen=True)
class RateLimit:
    requests: int
    per_seconds: int


class ScopeRecord(BaseModel):
    """Author-facing. One per target. `sensitivity` is deliberately NOT a field here --
    see the derived property below (R4/H7).

    `frozen=True` blocks attribute assignment after construction (fix round 1, I-1): without
    it, `record.basis = "client-authorised"` mutates a validated record into an unvalidated
    one in place, and `authorise()` would mint from whatever it was handed. `extra="forbid"`
    closes the matching hole on the input side -- an unexpected field is a typo, not silently
    ignored data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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

    @field_validator("target")
    @classmethod
    def _target_is_a_slug(cls, target: str) -> str:
        """Deferred Task 2 finding (ruling 3, 2026-09-28): the target names a scope file
        (`scopes/<target>.yaml`), a spec file, and a heartbeat key (Task 6). Anything outside
        `^[a-z0-9-]{1,64}$` -- spaces, uppercase, an unresolved `<placeholder>` -- is a mistake
        that should refuse at load rather than surface later as a path or key that never
        matches what another file expects."""
        if not _TARGET_SLUG.fullmatch(target):
            raise ValueError(f"target must match ^[a-z0-9-]{{1,64}}$, got {target!r}")
        return target

    @field_validator("user_agent_contact")
    @classmethod
    def _contact_is_a_working_https_url(cls, value: str) -> str:
        """Deferred Task 2 finding (ruling 3). Brief 3.3 requires a contact URL that resolves;
        an unresolved `<REAL-ACCOUNT>`-style placeholder is not a URL that could ever resolve,
        so it is refused here rather than loading as a plausible one (consistent with the
        placeholder test in tests/scope/test_records_load.py)."""
        if _URL_UNSAFE_CHARS.search(value):
            raise ValueError(f"user_agent_contact is not a well-formed https:// URL: {value!r}")
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError(f"user_agent_contact must be an https:// URL, got {value!r}")
        return value

    @field_validator("origins")
    @classmethod
    def _origins_are_exactly_scheme_host_port(cls, origins: list[str]) -> list[str]:
        """I-1. Each origin becomes a Chromium proxy-bypass rule, where `*`, `,` and `;` widen
        the allowlist. Validated, not rewritten: the authored value is what `record_sha256`
        hashes, and `scope.origin.parse_origin` canonicalises it wherever it is used."""
        for origin in origins:
            parse_origin(origin)
        return origins

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

    Identity on `_MINT` alone is not sufficient (fix round 1, I-2):
    `dataclasses.replace(an_authorised_target, origins=("*",))` passes the already-minted
    `_MINT` instance straight back through `__init__`, so re-minting via `replace()` outside
    the gate would otherwise succeed. `__post_init__` additionally requires the module-private
    `_MINTING` flag to be set, which only `gate.authorise()` ever sets, and only for the
    duration of its own `AuthorisedTarget(...)` call.
    """

    _token: _token_module._ScopeToken = field(repr=False, compare=False)
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
        # Identity, not isinstance (M29), AND the minting flag (fix round 1, I-2): identity
        # alone lets dataclasses.replace() re-mint outside the gate by passing the already
        # -minted token straight back through __init__.
        if self._token is not _token_module._MINT or not _token_module._MINTING.get():
            raise ScopeRefusal("not minted by the scope gate")
