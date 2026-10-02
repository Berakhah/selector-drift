"""The rules every registered mutation class obeys (Task 8's base.py docstring), checked over
the whole CATALOGUE so each class Tasks 9-11A register is enrolled the moment it is added."""

from __future__ import annotations

import random

import pytest

from testbench.mutations.base import CatalogueEntry, Mutation, parse, serialise
from testbench.mutations.catalogue import CATALOGUE
from testbench.portal.data import dataset_for
from testbench.portal.render import (
    render_detail,
    render_error,
    render_login,
    render_results,
    render_search,
)
from tests.testbench.helpers import FIXTURE_DAY

PAGE = render_results(FIXTURE_DAY)


def _detail() -> str:
    html = render_detail(FIXTURE_DAY, dataset_for(FIXTURE_DAY)[0].claim_number)
    assert html is not None
    return html


def every_page() -> list[str]:
    return [
        PAGE,
        render_results(FIXTURE_DAY, page=2),
        render_login(),
        render_search(),
        _detail(),
        render_error(404),
    ]


@pytest.mark.unit
@pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.name)
def test_the_entry_builds_a_mutation_named_after_its_class(entry: CatalogueEntry) -> None:
    mutation = entry.build(random.Random(0))
    assert isinstance(mutation, Mutation)
    assert mutation.name == entry.name == type(mutation).__name__


@pytest.mark.unit
@pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.name)
def test_a_fixed_rng_builds_the_same_mutation_and_the_same_page(entry: CatalogueEntry) -> None:
    """The schedule's promise (Task 8): a day's DOM is a pure function of its seed."""
    for seed in range(5):
        first = entry.build(random.Random(seed))
        second = entry.build(random.Random(seed))
        assert first.apply(PAGE) == second.apply(PAGE), seed


@pytest.mark.unit
@pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.name)
def test_the_results_page_really_changes(entry: CatalogueEntry) -> None:
    """Compared with the page after the same lxml round trip -- the raw Jinja output differs
    from any serialised page, so comparing with it would pass for a mutation that does nothing."""
    assert entry.build(random.Random(0)).apply(PAGE) != serialise(parse(PAGE))


@pytest.mark.unit
@pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.name)
def test_every_page_the_hook_mutates_survives(entry: CatalogueEntry) -> None:
    """The hook mutates EVERY text/html response (Task 8): login, search, detail and error
    pages have no results grid, and a class must pass them through rather than raise."""
    mutation = entry.build(random.Random(0))
    for html in every_page():
        out = mutation.apply(html)
        assert out.startswith("<!DOCTYPE html>")
        assert parse(out).find("body") is not None


@pytest.mark.unit
@pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.name)
def test_the_login_form_is_never_broken(entry: CatalogueEntry) -> None:
    """logged_in_page addresses the form by field name and submit button (Task 7); a real
    portal's UI change never breaks its own login."""
    doc = parse(entry.build(random.Random(0)).apply(render_login()))
    assert len(doc.xpath('//form//input[@name="username"]')) == 1
    assert len(doc.xpath('//form//input[@name="password"]')) == 1
    assert len(doc.xpath('//form//button[@type="submit"]')) == 1


@pytest.mark.unit
@pytest.mark.parametrize("entry", CATALOGUE, ids=lambda e: e.name)
def test_no_class_ever_writes_a_truth_marker(entry: CatalogueEntry) -> None:
    """The served page never carries data-sd-truth (Task 7). A mutation that minted one would
    hand the resolver the answer."""
    for seed in range(3):
        assert "data-sd-" not in entry.build(random.Random(seed)).apply(PAGE)
