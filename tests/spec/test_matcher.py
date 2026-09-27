import pytest

from selector_drift.spec.schema import (
    MAX_MATCH_LEN,
    FieldTarget,
    SpecError,
    compile_pattern,
    match_value,
    matches,
)

CLAIM = r"^[A-Z]{2}\d{8}$"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [("AB12345678", "match"), ("12345678", "no_match"), ("A" * (MAX_MATCH_LEN + 1), "too_long")],
)
def test_match_value_classifies_every_outcome(value, expected):
    assert match_value(CLAIM, value) == expected


@pytest.mark.unit
def test_rung_one_and_the_gate_share_one_bounded_matcher():
    # M23: spec.matches() is what rung 1 calls; it must carry the same cap as the gate.
    target = FieldTarget.model_validate(
        {
            "name": "claim_number",
            "description": "d",
            "relation": None,
            "assertions": [{"type": "format", "pattern": CLAIM}],
        }
    )
    assert matches(target, "AB12345678")
    assert not matches(target, "AB" + "1" * MAX_MATCH_LEN)


@pytest.mark.unit
def test_a_broken_pattern_is_a_spec_error_not_a_nightly_crash():
    with pytest.raises(SpecError, match="invalid format pattern"):
        compile_pattern("(unclosed")
