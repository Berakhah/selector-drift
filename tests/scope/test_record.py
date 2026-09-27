"""Deferred Task 2 finding (ruling 3, controller, 2026-09-28): `ScopeRecord.target` and
`.user_agent_contact` carried no format validation, so a placeholder like `<real-account>` in
`user_agent_contact` or a target with spaces/uppercase would validate and only fail later, deep
in the browser driver or the heartbeat keyed by target. Both are checked at load instead."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from selector_drift.scope.record import Basis, ScopeRecord


def rec(**kw: object) -> ScopeRecord:
    base: dict[str, object] = dict(
        target="testbench",
        basis=Basis.SELF_OWNED,
        authorising_party="me",
        authorised_on=dt.date(2026, 9, 1),
        expires_on=dt.date(2027, 9, 1),
        evidence_uri=None,
        evidence_sha256=None,
        origins=["https://bench.example.invalid"],
        rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
    )
    return ScopeRecord(**(base | kw))


@pytest.mark.unit
@pytest.mark.parametrize("target", ["testbench", "agent-perimeter", "a", "a" * 64])
def test_a_slug_shaped_target_is_accepted(target: str) -> None:
    assert rec(target=target).target == target


@pytest.mark.unit
@pytest.mark.parametrize(
    "target", ["Testbench", "agent_perimeter", "agent perimeter", "<real-account>", "", "a" * 65]
)
def test_a_non_slug_target_is_refused(target: str) -> None:
    with pytest.raises(ValidationError, match="target"):
        rec(target=target)


@pytest.mark.unit
@pytest.mark.parametrize(
    "url", ["https://example.invalid/contact", "https://github.com/some-org/selector-drift"]
)
def test_an_https_contact_url_is_accepted(url: str) -> None:
    assert rec(user_agent_contact=url).user_agent_contact == url


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/<REAL-ACCOUNT>/selector-drift",  # an unresolved placeholder
        "http://example.invalid/contact",  # not https
        "ftp://example.invalid/contact",
        "example.invalid/contact",  # no scheme at all
        "https://",  # no host
    ],
)
def test_a_non_https_or_placeholder_contact_url_is_refused(url: str) -> None:
    with pytest.raises(ValidationError, match="user_agent_contact"):
        rec(user_agent_contact=url)
