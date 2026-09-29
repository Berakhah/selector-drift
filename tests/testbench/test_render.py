from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from testbench.portal.data import COLUMNS
from testbench.portal.render import (
    PAGE_SIZE,
    render_error,
    render_login,
    render_results,
    render_search,
    result_set,
)
from tests.testbench.helpers import CLAIM_NUMBER, FIXTURE_DAY, headers, root, rows

PORTAL = Path(__file__).resolve().parents[2] / "testbench" / "portal"


@pytest.mark.unit
def test_results_page_has_six_headers_in_order() -> None:
    assert headers(render_results(FIXTURE_DAY)) == [label for _, label in COLUMNS]


@pytest.mark.unit
def test_results_page_serves_fifty_rows_and_numbered_pagination() -> None:
    html = render_results(FIXTURE_DAY)
    first = rows(html)
    assert len(first) == PAGE_SIZE
    assert all(CLAIM_NUMBER.fullmatch(r[0]) for r in first)
    pages = math.ceil(len(result_set(FIXTURE_DAY)) / PAGE_SIZE)
    assert len(root(html).xpath('//nav[@aria-label="Pagination"]//li')) == pages
    second = rows(render_results(FIXTURE_DAY, page=2))
    assert len(second) == PAGE_SIZE and second != first


@pytest.mark.unit
def test_the_truth_marker_appears_only_when_asked_for() -> None:
    """Task 8's answer key renders a marked copy to locate the true cells. The served page never
    carries the marker -- the resolver must not be able to key on the answer."""
    assert "data-sd-truth" not in render_results(FIXTURE_DAY)
    marked = root(render_results(FIXTURE_DAY, mark_truth=True))
    for key, _ in COLUMNS:
        assert len(marked.xpath("//*[@data-sd-truth=$k]", k=key)) == PAGE_SIZE


@pytest.mark.unit
def test_the_bench_ships_no_javascript_of_its_own() -> None:
    """Bench spec §3.2: the only script that can appear on a bench page is one a mutation
    injects (VirtualisedRows), so a mutation's effect is always the engine's."""
    files = [p for p in [*PORTAL.glob("templates/*"), *PORTAL.glob("static/**/*")] if p.is_file()]
    assert files
    assert not [p for p in files if p.suffix in {".js", ".mjs", ".ts", ".tsx", ".jsx"}]
    rendered = [render_results(FIXTURE_DAY), render_login(), render_search(), render_error(404)]
    texts = [p.read_text(encoding="utf-8").lower() for p in files] + [r.lower() for r in rendered]
    for text in texts:
        assert "<script" not in text
        assert "javascript:" not in text
        assert not re.search(r"\son[a-z]+\s*=", text)
