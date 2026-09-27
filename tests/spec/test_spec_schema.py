from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from selector_drift.spec.schema import (
    MAX_MATCH_LEN,
    AssertionSpec,
    FieldTarget,
    NavStep,
    Relation,
    TaskSpec,
    matches,
)

CLAIM = FieldTarget(
    name="claim_number",
    description="the claim number cell",
    relation=Relation.RIGHT_OF,
    assertions=[AssertionSpec(type="format", pattern=r"^[A-Z]{2}\d{8}$")],
)


def _spec(**overrides: Any) -> TaskSpec:
    base: dict[str, Any] = {
        "version": 1,
        "scope_ref": "scopes/testbench.yaml",
        "schedule": "12 4 * * *",
        "row_scope": "each row of the results table",
        "natural_key": ["claim_number"],
        "expected_schema": {"claim_number": "string"},
        "navigate": [{"step": "goto", "path": "/results"}],
        "targets": [
            {
                "name": "claim_number",
                "description": "the claim number cell",
                "assertions": [{"type": "format", "pattern": r"^[A-Z]{2}\d{8}$"}],
            }
        ],
    }
    return TaskSpec.model_validate(base | overrides)


@pytest.mark.unit
def test_a_format_assertion_needs_a_compilable_pattern() -> None:
    with pytest.raises(ValidationError, match="does not compile"):
        AssertionSpec(type="format", pattern="([A-Z")


@pytest.mark.unit
def test_an_assertion_carries_exactly_the_payload_its_type_names() -> None:
    """A format assertion with a stray `rule` is a spec author's mistake, not a second check."""
    with pytest.raises(ValidationError, match="takes exactly `pattern`"):
        AssertionSpec(type="format", pattern="^x$", rule={"min": 1.0})
    with pytest.raises(ValidationError, match="takes exactly `rule`"):
        AssertionSpec(type="domain")


@pytest.mark.unit
def test_an_unknown_assertion_type_is_refused_at_load() -> None:
    """An unknown type is a refusal, not a skip (00 section 7)."""
    with pytest.raises(ValidationError):
        AssertionSpec.model_validate({"type": "vibes"})


@pytest.mark.unit
def test_a_domain_rule_is_checked_for_sense() -> None:
    with pytest.raises(ValidationError, match="min"):
        AssertionSpec(type="domain", rule={"min": 10.0, "max": 1.0})
    with pytest.raises(ValidationError, match="at least one"):
        AssertionSpec(type="domain", rule={})
    with pytest.raises(ValidationError):
        AssertionSpec(type="domain", rule={"date_after": "01/01/2023"})  # not ISO
    with pytest.raises(ValidationError):
        AssertionSpec.model_validate({"type": "domain", "rule": {"between": [1, 2]}})


@pytest.mark.unit
def test_a_cross_field_expression_must_parse_and_is_never_evaluated_here() -> None:
    """R13/H5. Parsing proves it is an expression; Task 21's safe_eval decides what it may do."""
    with pytest.raises(ValidationError, match="does not parse"):
        AssertionSpec(type="cross_field", expression="net + ")
    ok = AssertionSpec(type="cross_field", expression="net + tax == gross")
    assert ok.expression == "net + tax == gross"


@pytest.mark.unit
@pytest.mark.parametrize("path", ["https://evil.example/x", "//evil.example/x", "login"])
def test_a_goto_step_takes_a_same_origin_path_never_a_url(path: str) -> None:
    """R8/H2. An absolute URL in a spec walks off the authorised target."""
    with pytest.raises(ValidationError, match="never a URL"):
        NavStep(step="goto", path=path)


@pytest.mark.unit
def test_each_step_kind_takes_exactly_its_own_keys() -> None:
    assert NavStep(step="goto", path="/").path == "/"
    assert NavStep(step="fill", field="username", value_from="credentials.username").field
    assert NavStep(step="click", role="button", name="Sign in").name == "Sign in"
    with pytest.raises(ValidationError):
        NavStep(step="fill", field="username")  # no value_from
    with pytest.raises(ValidationError):
        NavStep(step="click", role="button", name="Go", path="/x")  # stray path
    with pytest.raises(ValidationError):
        NavStep(step="fill", field="input[name=user]", value_from="credentials.username")
    with pytest.raises(ValidationError):
        NavStep.model_validate({"step": "fill", "field": "pw", "value_from": "env.PASSWORD"})


@pytest.mark.unit
def test_a_description_with_selector_syntax_is_refused() -> None:
    with pytest.raises(ValidationError, match="in words"):
        FieldTarget(name="x", description="table#results > td[2]", assertions=[])


@pytest.mark.unit
def test_unknown_keys_are_refused_not_ignored() -> None:
    """A misspelt `asertions` would otherwise load with no checks at all."""
    with pytest.raises(ValidationError):
        FieldTarget.model_validate(
            {
                "name": "x",
                "description": "x",
                "assertions": [],
                "asertions": [{"type": "format", "pattern": "^x$"}],
            }
        )
    with pytest.raises(ValidationError):
        _spec(sensitivity="SYNTHETIC")


@pytest.mark.unit
def test_the_natural_key_must_be_in_the_schema_and_extracted() -> None:
    """M5. Task 28 upserts on it; a key nobody extracts cannot deduplicate anything."""
    with pytest.raises(ValidationError, match="expected_schema"):
        _spec(natural_key=["policy_id"])
    with pytest.raises(ValidationError, match="names no target"):
        _spec(
            expected_schema={"claim_number": "string", "policy_id": "string"},
            natural_key=["policy_id"],
        )


@pytest.mark.unit
def test_duplicate_target_names_are_refused() -> None:
    t = {"name": "claim_number", "description": "the claim number cell", "assertions": []}
    with pytest.raises(ValidationError, match="duplicate"):
        _spec(targets=[t, t])


@pytest.mark.unit
@pytest.mark.parametrize("ref", ["../scopes/x.yaml", "/etc/scopes/x.yaml", "scopes/x.yml"])
def test_scope_ref_names_a_file_under_scopes(ref: str) -> None:
    with pytest.raises(ValidationError, match="scope_ref"):
        _spec(scope_ref=ref)


@pytest.mark.unit
def test_schedule_is_five_cron_fields() -> None:
    assert _spec(schedule="12 4 * * *").schedule == "12 4 * * *"
    for bad in ("12 4 * *", "@daily", "12 4 * * * *"):
        with pytest.raises(ValidationError, match="five-field"):
            _spec(schedule=bad)


@pytest.mark.unit
def test_matches_checks_every_format_assertion() -> None:
    two = CLAIM.model_copy(
        update={"assertions": [*CLAIM.assertions, AssertionSpec(type="format", pattern=r"^AB.*$")]}
    )
    assert matches(CLAIM, "AB12345678") and matches(CLAIM, "  AB12345678 ")
    assert not matches(CLAIM, "12345678")
    assert matches(two, "AB12345678") and not matches(two, "CD12345678")


@pytest.mark.unit
def test_matches_bounds_its_input() -> None:
    """A crafted page value against a backtracking pattern must not hang rung 1."""
    evil = FieldTarget(
        name="x", description="x", assertions=[AssertionSpec(type="format", pattern=r"^(a+)+$")]
    )
    assert not matches(evil, "a" * (MAX_MATCH_LEN + 1))


@pytest.mark.unit
def test_matches_with_no_format_assertion_has_nothing_to_contradict() -> None:
    only_domain = FieldTarget(
        name="x", description="x", assertions=[AssertionSpec(type="domain", rule={"min": 0.0})]
    )
    assert matches(only_domain, "anything")


@pytest.mark.unit
def test_a_field_may_name_the_portals_visible_label() -> None:
    """The label rungs look for, when it is not the humanised field name (2026-09-26)."""
    t = FieldTarget(
        name="amount", description="the claim amount", label="Claim Amount (USD)", assertions=[]
    )
    assert t.label == "Claim Amount (USD)"
    assert FieldTarget(name="amount", description="the claim amount", assertions=[]).label is None
