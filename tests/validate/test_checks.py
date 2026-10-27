# tests/validate/test_checks.py
import pytest

from selector_drift.scope.record import FirstHealPolicy
from selector_drift.spec.schema import AssertionSpec, FieldTarget, SpecError, TaskSpec
from selector_drift.validate.assertions.cross_field import UnsafeExpression
from selector_drift.validate.checks import checks_for
from tests.validate.helpers import ax, heuristic

SPEC = TaskSpec.model_validate(
    {
        "version": 2,
        "scope_ref": "scopes/testbench.yaml",
        "schedule": "12 4 * * *",
        "row_scope": "each row",
        "natural_key": ["claim_number"],
        "expected_schema": {"claim_number": "string", "amount": "decimal", "net": "decimal"},
        "navigate": [],
        "targets": [
            {
                "name": "claim_number",
                "description": "d",
                "assertions": [{"type": "format", "pattern": r"^[A-Z]{2}\d{8}$"}],
            },
            {
                "name": "amount",
                "description": "d",
                "distributional": True,
                "assertions": [
                    {"type": "domain", "rule": {"min": 100.0, "max": 250000.0}},
                    {"type": "cross_field", "expression": "amount >= net"},
                ],
            },
            {"name": "net", "description": "d", "assertions": []},
        ],
    }
)
CLAIM, AMOUNT, NET = SPEC.targets
REQUIRED = FirstHealPolicy.REQUIRED


def names(field, **kw):
    return [
        c.name
        for c in checks_for(
            SPEC,
            field,
            [ax("n"), heuristic("n")],
            warmup_runs=kw.get("warmup_runs", 5),
            first_heal_policy=kw.get("policy", REQUIRED),
        )
    ]


def _field_with(**assertion) -> FieldTarget:
    spec = AssertionSpec.model_construct(
        **({"pattern": None, "rule": None, "expression": None} | assertion)
    )
    return FieldTarget.model_construct(
        name="claim_number", description="d", relation=None, assertions=[spec], distributional=False
    )


@pytest.mark.unit
def test_an_unknown_assertion_type_is_refused_not_skipped():
    with pytest.raises(SpecError, match="unknown assertion type"):
        checks_for(SPEC, _field_with(type="vibes"), [], warmup_runs=5, first_heal_policy=REQUIRED)


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["format", "domain", "cross_field"])
def test_an_assertion_missing_its_argument_is_refused(kind):
    with pytest.raises(SpecError, match="needs"):
        checks_for(SPEC, _field_with(type=kind), [], warmup_runs=5, first_heal_policy=REQUIRED)


@pytest.mark.unit
def test_every_field_gets_both_confirmers_and_the_first_heal_check():
    # CLAIM is the first target, so it also carries the run-level RowCountContinuity.
    assert names(CLAIM) == [
        "FormatAssertion",
        "IndependentRungAgreement",
        "RowKeyContinuity",
        "RowCountContinuity",
        "FirstHealConfirmation",
    ]


@pytest.mark.unit
def test_a_distributional_field_gets_every_kind_in_a_fixed_order():
    assert names(AMOUNT) == [
        "DomainAssertion",
        "CrossFieldAssertion",
        "IndependentRungAgreement",
        "RowKeyContinuity",
        "DistributionalAssertion",
        "FirstHealConfirmation",
    ]


@pytest.mark.unit
def test_warm_up_and_policy_come_from_the_caller_not_from_defaults():
    checks = checks_for(
        SPEC, AMOUNT, [], warmup_runs=5, first_heal_policy=FirstHealPolicy.AUTO_PROMOTE
    )
    by_name = {c.name: c for c in checks}
    assert by_name["DistributionalAssertion"].warmup_runs == 5
    assert by_name["FirstHealConfirmation"].policy is FirstHealPolicy.AUTO_PROMOTE


@pytest.mark.unit
def test_the_confirming_rungs_are_injected_not_imported():
    rungs = [ax("n"), heuristic("n")]
    ira = next(
        c
        for c in checks_for(SPEC, CLAIM, rungs, warmup_runs=5, first_heal_policy=REQUIRED)
        if c.name == "IndependentRungAgreement"
    )
    assert list(ira.rungs) == rungs


@pytest.mark.unit
def test_a_cross_field_expression_naming_an_unextracted_field_is_refused():
    field = _field_with(type="cross_field", expression="amount == gross")
    with pytest.raises(SpecError, match="gross"):
        checks_for(SPEC, field, [], warmup_runs=5, first_heal_policy=REQUIRED)


@pytest.mark.unit
def test_a_hostile_expression_in_a_spec_is_a_spec_refusal():
    field = _field_with(type="cross_field", expression="__import__('os') == 1")
    with pytest.raises(UnsafeExpression):
        checks_for(SPEC, field, [], warmup_runs=5, first_heal_policy=REQUIRED)


@pytest.mark.unit
def test_a_spec_file_with_an_unknown_assertion_type_does_not_load():
    """The loader half (2026-09-26): AssertionSpec.type is a Literal, so `vibes` never reaches
    checks_for from a file -- model_construct above is the only way to test checks_for's own
    refusal, and this proves the file path refuses first."""
    from selector_drift.spec.loader import load_spec_text

    text = (
        "version: 1\nscope_ref: scopes/testbench.yaml\nschedule: '12 4 * * *'\n"
        "row_scope: each row\nnatural_key: [claim_number]\n"
        "expected_schema: {claim_number: string}\nnavigate: []\n"
        "targets:\n  - name: claim_number\n    description: d\n"
        "    assertions: [{type: vibes}]\n"
    )
    with pytest.raises(SpecError):
        load_spec_text(text)


@pytest.mark.unit
@pytest.mark.parametrize("field", [CLAIM, AMOUNT, NET], ids=lambda f: f.name)
def test_first_heal_confirmation_is_always_present_and_last(field):
    """Task 20 ledger: the gate's strictness is only as good as this guarantee, so pin it,
    including for a field with no declared assertions."""
    assert names(field)[-1] == "FirstHealConfirmation"
    assert names(field).count("FirstHealConfirmation") == 1


@pytest.mark.unit
def test_the_standalone_and_the_embedded_row_key_check_are_one_object():
    checks = checks_for(
        SPEC, AMOUNT, [ax("n"), heuristic("n")], warmup_runs=5, first_heal_policy=REQUIRED
    )
    ira = next(c for c in checks if c.name == "IndependentRungAgreement")
    standalone = next(c for c in checks if c.name == "RowKeyContinuity")
    assert ira.value is standalone
