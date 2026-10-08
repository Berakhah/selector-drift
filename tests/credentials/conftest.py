import datetime as dt

import pytest

from selector_drift.scope.gate import authorise
from selector_drift.scope.record import AuthorisedTarget, Basis, ScopeRecord

SCOPE_YAML = """target: testbench
basis: self-owned
authorising_party: Acme Claims Ltd
authorised_on: 2026-01-01
expires_on: 2027-12-31
evidence_uri: null
evidence_sha256: null
origins: [ https://bench.example.invalid ]
rate_limit: { requests: 1, per_seconds: 30 }
user_agent_contact: https://example.invalid/contact
first_heal_policy: auto_promote
legal_review: self_drafted
"""


@pytest.fixture
def target() -> AuthorisedTarget:
    """Minted through `authorise()` like every other target -- never through the token."""
    rec = ScopeRecord(
        target="testbench",
        basis=Basis.SELF_OWNED,
        authorising_party="me",
        authorised_on=dt.date(2026, 1, 1),
        expires_on=dt.date(2027, 12, 31),
        evidence_uri=None,
        evidence_sha256=None,
        origins=["https://bench.example.invalid"],
        rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
    )
    return authorise(rec, today=dt.date(2026, 9, 7))
