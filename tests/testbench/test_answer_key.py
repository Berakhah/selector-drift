from __future__ import annotations

import random

import lxml.html
import pytest

from testbench.answers.key import FIELDS, expected_selector, expected_values, expected_xpaths
from testbench.mutations.base import CatalogueEntry, parse, serialise
from testbench.mutations.catalogue import CATALOGUE
from testbench.portal.hook import mutate_html
from testbench.portal.render import PAGE_SIZE, render_results
from tests.testbench.helpers import FIXTURE_DAY, WRAP_TABLE, accessible_text, bench_catalogue, rows

EVERY_ENTRY = (*CATALOGUE, *bench_catalogue(), *WRAP_TABLE)


@pytest.mark.unit
def test_expected_values_are_what_the_unmutated_page_shows() -> None:
    page = rows(render_results(FIXTURE_DAY))
    for index, field in enumerate(FIELDS):
        assert expected_values(FIXTURE_DAY, field) == [r[index] for r in page]


@pytest.mark.unit
@pytest.mark.parametrize("field", ["claim_number", "amount"])
def test_expected_xpaths_point_at_the_true_cells_on_the_served_page(field: str) -> None:
    served = parse(mutate_html(FIXTURE_DAY, render_results(FIXTURE_DAY), catalogue=WRAP_TABLE))
    tree = served.getroottree()
    paths = expected_xpaths(FIXTURE_DAY, field, catalogue=WRAP_TABLE)
    assert len(paths) == PAGE_SIZE and all("div" in p for p in paths)  # the wrap moved them
    found = [tree.xpath(p) for p in paths]
    assert all(len(nodes) == 1 for nodes in found)
    texts = [accessible_text(nodes[0]) for nodes in found]
    assert texts == expected_values(FIXTURE_DAY, field)


@pytest.mark.unit
def test_expected_selector_is_the_first_rows_node() -> None:
    assert (
        expected_selector(FIXTURE_DAY, "claim_number")
        == expected_xpaths(FIXTURE_DAY, "claim_number")[0]
    )


@pytest.mark.unit
def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(KeyError):
        expected_xpaths(FIXTURE_DAY, "prior_amount")


@pytest.mark.unit
@pytest.mark.parametrize("entry", EVERY_ENTRY, ids=lambda e: e.name)
def test_the_truth_marker_changes_attributes_only(entry: CatalogueEntry) -> None:
    """Every class in the catalogue -- Tasks 9-11A add theirs and are covered automatically --
    must (a) keep data-sd-truth on every true cell, (b) never copy it onto a clone, and
    (c) not behave differently because it is present. Otherwise the key describes another page."""
    mutation = entry.build(random.Random(0))
    marked = parse(mutation.apply(render_results(FIXTURE_DAY, mark_truth=True)))
    plain = mutation.apply(render_results(FIXTURE_DAY))
    for field in FIELDS:
        assert len(marked.xpath("//*[@data-sd-truth=$f]", f=field)) == PAGE_SIZE, field
    for el in marked.xpath("//*[@data-sd-truth]"):
        if isinstance(el, lxml.html.HtmlElement):
            del el.attrib["data-sd-truth"]
    assert serialise(marked) == serialise(parse(plain))
