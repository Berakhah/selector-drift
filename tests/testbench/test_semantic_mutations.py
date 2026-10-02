"""Semantic mutation classes: LabelReword, IdChurn, FrameworkMarkup, Pagination (Task 10)."""

from __future__ import annotations

import math
import random
import re

import pytest

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.column_reorder import ColumnReorder
from testbench.mutations.dom_restructure import DomRestructure
from testbench.mutations.framework_markup import DIALECTS, FrameworkMarkup
from testbench.mutations.grid import header_cells, header_row
from testbench.mutations.id_churn import SCHEMES, IdChurn
from testbench.mutations.label_reword import LabelReword
from testbench.mutations.pagination import Pagination
from testbench.mutations.table_to_div import TableToDiv
from testbench.mutations.vocabulary import FIELD_ORDER, SYNONYMS, find_column, wordings
from testbench.portal.data import COLUMNS, dataset_for
from testbench.portal.render import (
    PAGE_SIZE,
    SearchQuery,
    render_detail,
    render_results,
    result_set,
)
from tests.testbench.helpers import FIXTURE_DAY, cells, column, headers, root, rows

LABELS = [label for _, label in COLUMNS]
BEFORE = render_results(FIXTURE_DAY)
SEMANTIC: list[MutationBase] = [LabelReword(), IdChurn(), FrameworkMarkup(), Pagination()]


def ids(html: str) -> list[str]:
    return [
        str(el.get("id")) for el in root(html).iter() if isinstance(el.tag, str) and el.get("id")
    ]


def detail_page() -> str:
    html = render_detail(FIXTURE_DAY, dataset_for(FIXTURE_DAY)[0].claim_number)
    assert html is not None
    return html


@pytest.mark.unit
@pytest.mark.parametrize("mutation", SEMANTIC, ids=lambda m: m.name)
def test_mutation_changes_markup_but_preserves_values(mutation: MutationBase) -> None:
    after = mutation.apply(BEFORE)
    assert after != serialise(parse(BEFORE))
    assert sorted(cells(after)) == sorted(cells(BEFORE))
    assert rows(after) == rows(BEFORE)  # same values, same rows, same order


@pytest.mark.unit
def test_label_reword_changes_the_header_and_nothing_else() -> None:
    after = LabelReword().apply(BEFORE)
    assert "Claim No." in headers(after) and "Claim Number" not in headers(after)
    assert headers(after) == [SYNONYMS[key][0] for key in FIELD_ORDER]
    assert rows(after) == rows(BEFORE)


@pytest.mark.unit
def test_label_reword_rewords_the_detail_pages_labels_too() -> None:
    """The detail page is rung 3's "value to the right of the label" case (bench spec §3.2)."""
    doc = root(LabelReword().apply(detail_page()))
    assert [dt.text_content() for dt in doc.xpath("//dl/dt")] == [
        SYNONYMS[key][0] for key in FIELD_ORDER
    ]
    assert [dd.text_content() for dd in doc.xpath("//dl/dd")] == [
        dd.text_content() for dd in root(detail_page()).xpath("//dl/dd")
    ]


@pytest.mark.unit
def test_label_reword_is_idempotent() -> None:
    once = LabelReword().apply(BEFORE)
    assert LabelReword().apply(once) == once


@pytest.mark.unit
def test_label_reword_draws_its_wording_from_the_rng() -> None:
    drawn = [LabelReword.from_rng(random.Random(seed)) for seed in range(20)]
    assert len({m.renames for m in drawn}) >= 10
    for mutation in drawn:
        for old, new in mutation.renames:
            key = FIELD_ORDER[LABELS.index(old)]
            assert new in SYNONYMS[key]
    with pytest.raises(ValueError, match="differ"):
        LabelReword(renames=(("Amount", "Amount"),))


@pytest.mark.unit
def test_label_reword_follows_the_grid_through_structural_changes() -> None:
    for first in (ColumnReorder(), DomRestructure(), TableToDiv()):
        after = LabelReword().apply(first.apply(BEFORE))
        for key, label in COLUMNS:
            assert column(after, SYNONYMS[key][0]) == column(BEFORE, label), (first.name, key)


@pytest.mark.unit
def test_every_header_wording_names_exactly_one_column() -> None:
    """The adversarial classes find their column by wording after LabelReword may have run, so
    no wording may belong to two columns or contain another column's label as a phrase."""
    for key in FIELD_ORDER:
        for other in FIELD_ORDER:
            if other == key:
                continue
            assert not wordings(key) & wordings(other), (key, other)
            phrase = re.compile(rf"{re.escape(dict(COLUMNS)[other])}")
            assert not any(phrase.search(w) for w in wordings(key)), (key, other)
    for page in (BEFORE, LabelReword.from_rng(random.Random(3)).apply(BEFORE)):
        head = header_row(parse(page).xpath("//table")[0])
        assert head is not None
        assert [find_column(header_cells(head), key) for key in FIELD_ORDER] == list(range(6))


@pytest.mark.unit
@pytest.mark.parametrize("scheme", SCHEMES)
def test_id_churn_regenerates_every_id(scheme: str) -> None:
    before_ids, after = ids(BEFORE), IdChurn(scheme=scheme, token=42).apply(BEFORE)
    assert before_ids == ["main", "results"]
    assert len(ids(after)) == len(before_ids) and not set(ids(after)) & set(before_ids)
    assert len(set(ids(after))) == len(ids(after))


@pytest.mark.unit
def test_id_churn_rewrites_every_reference_with_its_id() -> None:
    page = (
        '<!DOCTYPE html><html><body><label for="q">Find</label><input id="q" name="q">'
        '<p id="help">Type a number</p><input name="r" aria-describedby="help q">'
        '<a href="#help">Help</a><a href="/results#top">Top</a></body></html>'
    )
    doc = root(IdChurn(scheme="suffix", token=255).apply(page))
    assert doc.xpath("//label")[0].get("for") == "q-0ff"
    assert doc.xpath('//input[@name="r"]')[0].get("aria-describedby") == "help-0ff q-0ff"
    assert [a.get("href") for a in doc.xpath("//a")] == ["#help-0ff", "/results#top"]


@pytest.mark.unit
@pytest.mark.parametrize("dialect", DIALECTS)
def test_framework_markup_re_emits_the_page_in_each_dialect(dialect: str) -> None:
    after = FrameworkMarkup(dialect=dialect, scope="0badc0de").apply(BEFORE)
    doc = root(after)
    main = doc.xpath("//main")[0]
    framework_root = main[0]
    assert (
        len(main) == 1
        and framework_root.tag == {"vue": "div", "angular": "app-root", "react": "div"}[dialect]
    )
    scoped = {"vue": "data-v-0badc0de", "angular": "_ngcontent-ng-c0badc0de", "react": None}[
        dialect
    ]
    descendants = [el for el in framework_root.iterdescendants() if isinstance(el.tag, str)]
    if scoped is not None:
        assert all(el.get(scoped) == "" for el in descendants)
    comments = [c.text for c in doc.xpath("//tbody/comment()")]
    assert comments == {"vue": ["[", "]"], "angular": [], "react": ["$", "/$"]}[dialect]
    assert rows(after) == rows(BEFORE)


@pytest.mark.unit
def test_framework_markup_draws_dialect_and_scope_from_the_rng() -> None:
    drawn = {FrameworkMarkup.from_rng(random.Random(seed)) for seed in range(20)}
    assert len(drawn) == 20 and {m.dialect for m in drawn} == set(DIALECTS)


def _closed_claims_page_two() -> tuple[str, int]:
    query = SearchQuery.parse(status="Closed")
    pages = math.ceil(len(result_set(FIXTURE_DAY, query)) / PAGE_SIZE)
    assert pages >= 3, "the fixture needs a middle page"
    return render_results(FIXTURE_DAY, page=2, query=query), pages


@pytest.mark.unit
def test_pagination_prev_next_keeps_the_search_and_the_position() -> None:
    page, pages = _closed_claims_page_two()
    doc = root(Pagination(style="prev-next").apply(page))
    assert not doc.xpath('//nav[@aria-label="Pagination"]')
    pager = doc.xpath('//nav[@aria-label="Results pages"]')[0]
    assert [(a.text, a.get("href"), a.get("rel")) for a in pager.xpath(".//a")] == [
        ("« Previous", "/results?status=Closed&page=1", "prev"),
        ("Next »", "/results?status=Closed&page=3", "next"),
    ]
    assert pager.xpath('.//span[@class="pager-status"]')[0].text == f"Page 2 of {pages}"


@pytest.mark.unit
def test_pagination_select_offers_every_page_without_javascript() -> None:
    page, pages = _closed_claims_page_two()
    doc = root(Pagination(style="select").apply(page))
    form = doc.xpath('//form[@class="pager"]')[0]
    assert (form.get("method"), form.get("action")) == ("get", "/results")
    assert [(i.get("name"), i.get("value")) for i in form.xpath('.//input[@type="hidden"]')] == [
        ("status", "Closed")
    ]
    options = form.xpath('.//select[@name="page"]/option')
    assert len(options) == pages
    assert [o.get("value") for o in options if o.get("selected")] == ["2"]


@pytest.mark.unit
def test_pagination_load_more_links_the_next_page_only() -> None:
    page, _ = _closed_claims_page_two()
    doc = root(Pagination(style="load-more").apply(page))
    assert [a.get("href") for a in doc.xpath('//p[@class="load-more"]/a')] == [
        "/results?status=Closed&page=3"
    ]
    assert not doc.xpath("//nav[@aria-label]//li")


@pytest.mark.unit
def test_pagination_can_move_the_pager_above_the_grid() -> None:
    doc = root(Pagination(style="prev-next", placement="before").apply(BEFORE))
    pager, table = doc.xpath('//nav[@class="pager"]')[0], doc.xpath("//table")[0]
    assert pager.getnext() is table


@pytest.mark.unit
def test_pagination_is_idempotent() -> None:
    once = Pagination(style="select").apply(BEFORE)
    assert Pagination(style="select").apply(once) == once
