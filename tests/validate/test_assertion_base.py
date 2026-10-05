import pytest

from selector_drift.validate.assertions.base import Cell, cells, column, preview
from tests.validate.helpers import AT, candidate


@pytest.mark.unit
def test_cells_read_value_time_and_selector_and_skip_rows_without_the_field():
    c = candidate([{"amount": "1.00"}, {"status": "OPEN"}, {"amount": "2.00"}], selector="#a")
    assert cells(c, "amount") == [Cell("1.00", AT, "#a"), Cell("2.00", AT, "#a")]
    assert column(c, "amount") == ["1.00", "2.00"] and column(c, "nope") == []


@pytest.mark.unit
def test_preview_bounds_what_a_verdict_copies_out_of_a_page():
    assert preview(["a", "b"]) == "'a', 'b'"
    assert preview(["1", "2", "3", "4", "5"]) == "'1', '2', '3' (+2 more)"
    assert preview(["x" * 50], width=4) == "'xxxx...'"
