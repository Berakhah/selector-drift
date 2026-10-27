"""A client target cannot quietly lower the controls that protect the client's system.

`first_heal_policy: auto_promote` skips the human confirmation of a first heal, and a short
`warmup_runs` shortens the window in which the distributional check has no basis. Both are right
for the synthetic bench and wrong for a client's portal, so the record refuses them at load,
whatever a YAML file says. Until now only a test over the repo's own scope files held that line.

Callers: none (pytest collects tests/scope/test_*.py); exercises scope.record.ScopeRecord.
Overlap: none (tests/scope/test_gate.py only checks warmup_runs >= 1). No data files read.
Instruction: "go ahead".
"""

import datetime as dt

import pytest
from pydantic import ValidationError

from selector_drift.scope.record import CLIENT_MIN_WARMUP_RUNS, Basis, ScopeRecord


def rec(**kw):
    base = dict(
        target="a-client",
        basis=Basis.CLIENT_AUTHORISED,
        authorising_party="the client",
        authorised_on=dt.date(2026, 9, 1),
        expires_on=dt.date(2027, 9, 1),
        evidence_uri="file:///signed.pdf",
        evidence_sha256="0" * 64,
        origins=["https://portal.example.invalid"],
        rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
    )
    return ScopeRecord(**(base | kw))


@pytest.mark.unit
def test_a_client_record_cannot_auto_promote_first_heals():
    with pytest.raises(ValidationError, match="first_heal_policy"):
        rec(first_heal_policy="auto_promote")


@pytest.mark.unit
def test_a_client_record_cannot_shorten_the_warmup_below_the_floor():
    with pytest.raises(ValidationError, match="warmup_runs"):
        rec(warmup_runs=CLIENT_MIN_WARMUP_RUNS - 1)


@pytest.mark.unit
def test_a_client_record_may_lengthen_the_warmup():
    assert rec(warmup_runs=CLIENT_MIN_WARMUP_RUNS).warmup_runs == CLIENT_MIN_WARMUP_RUNS
    assert rec(warmup_runs=90).warmup_runs == 90


@pytest.mark.unit
def test_the_floor_is_the_documented_client_warmup():
    assert CLIENT_MIN_WARMUP_RUNS == 30  # methodology.md: bench 5 runs, client targets 30


@pytest.mark.unit
def test_a_self_owned_record_keeps_the_bench_settings():
    r = rec(
        basis=Basis.SELF_OWNED,
        evidence_uri=None,
        evidence_sha256=None,
        first_heal_policy="auto_promote",
        warmup_runs=5,
    )
    assert r.first_heal_policy == "auto_promote"
    assert r.warmup_runs == 5
