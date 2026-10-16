"""Task 26B: every benign heal is ACCEPTED by the gate (R14, C3).

The complement of test_adversarial_refused. A gate that quarantines every legitimate heal passes
every other test in the plan and is worthless; this suite is the one that says the product works.
"""

import pytest

from selector_drift.resolve.trace import Family
from selector_drift.validate.gate import ValidatedExtraction
from tests.suite.conftest import (
    ALL_BENIGN_MUTATION_CLASSES,
    confirmer_of,
    family_of,
    healed_fields,
    run_pipeline_on,
    verdicts_of,
)


def _failing(result) -> list[tuple[str | None, str, str]]:
    return [(v.field, v.check, v.reason) for v in verdicts_of(result) if v.outcome.name != "PASS"]


@pytest.mark.unit
@pytest.mark.parametrize("m", ALL_BENIGN_MUTATION_CLASSES, ids=lambda m: m.name)
def test_every_benign_mutation_is_healed_and_accepted(m, bench_evidence, history, warm_cache):
    result = run_pipeline_on(bench_evidence[m.name], history=history, cache=warm_cache())
    assert isinstance(result, ValidatedExtraction), _failing(result)


@pytest.mark.unit
@pytest.mark.parametrize("m", ALL_BENIGN_MUTATION_CLASSES, ids=lambda m: m.name)
def test_each_acceptance_names_the_confirmer_that_carried_it(
    m, bench_evidence, history, warm_cache
):
    # An acceptance with no named confirmer is a check that silently passed.
    cache = warm_cache()
    result = run_pipeline_on(bench_evidence[m.name], history=history, cache=cache)
    for field, winner in healed_fields(bench_evidence[m.name], cache):
        c = confirmer_of(result, field)
        assert c is not None, f"{m.name}: {field} healed by rung {winner} with no confirmer"
        assert c.family is not family_of(winner)  # cross-family, always


@pytest.mark.unit
def test_the_label_reword_case_specifically(bench_evidence, history, warm_cache):
    # C3: the most common real UI change. Rungs 2 and 3 look for the old label and find nothing,
    # so only the VALUE family can confirm rung 4's heal.
    result = run_pipeline_on(bench_evidence["LabelReword"], history=history, cache=warm_cache())
    assert isinstance(result, ValidatedExtraction), _failing(result)
    c = confirmer_of(result, "claim_number")
    assert c is not None and c.family is Family.VALUE


@pytest.mark.unit
def test_the_quarantine_rate_on_benign_mutations_is_zero(bench_evidence, history, warm_cache):
    # The number that goes in the methodology next to the heal count.
    q = [
        m.name
        for m in ALL_BENIGN_MUTATION_CLASSES
        if not isinstance(
            run_pipeline_on(bench_evidence[m.name], history=history, cache=warm_cache()),
            ValidatedExtraction,
        )
    ]
    assert q == [], f"benign mutations quarantined: {q}"
