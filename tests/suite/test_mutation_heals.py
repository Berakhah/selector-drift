import pytest

from selector_drift.heallog.replay import replay
from tests.suite.conftest import ALL_BENIGN_MUTATION_CLASSES, AMOUNT, CLAIM_NUMBER


@pytest.mark.unit
@pytest.mark.parametrize("m", ALL_BENIGN_MUTATION_CLASSES, ids=lambda m: m.name)
@pytest.mark.parametrize("field", [CLAIM_NUMBER, AMOUNT], ids=lambda f: f.name)
def test_every_mutation_class_heals_to_the_right_element(
    m, field, bench_evidence, truth, warm_cache
):
    el, trace = replay(bench_evidence[m.name], field, warm_cache())
    # C5: node identity, never selector text. replay() uses a StaticPageSource, whose node_id is
    # lxml's getpath() (Task 13) -- the same call that built `truth`. Any row's cell in the true
    # column is the true column.
    assert el.node_id in truth[m.name][field.name]
    # Rung 1 may legitimately win when a mutation does not touch this field's cell and its
    # anchor still verifies (Task 17). Rung 5 may never: it escalates, it does not heal.
    assert trace.winner in (1, 2, 3, 4), "rungs 1-4 must carry this; rung 5 escalates"
