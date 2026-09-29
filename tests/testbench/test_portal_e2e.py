from __future__ import annotations

import csv
import io
import re

import pytest
from playwright.sync_api import Browser, expect

from testbench.portal.data import COLUMNS
from tests.testbench.helpers import CLAIM_NUMBER, logged_in_page

LABELS = [label for _, label in COLUMNS]


@pytest.mark.e2e
def test_login_is_required_for_results(browser: Browser, bench_server: str) -> None:
    context = browser.new_context(base_url=bench_server)
    page = context.new_page()
    page.goto("/results")
    expect(page).to_have_url(re.compile(r"/login$"))
    context.close()


@pytest.mark.e2e
def test_results_table_has_six_columns_of_synthetic_claims(
    browser: Browser, bench_server: str
) -> None:
    page = logged_in_page(browser, bench_server)
    page.goto("/results")
    expect(page.get_by_role("columnheader")).to_have_count(6)
    expect(page.get_by_role("cell").first).to_have_text(CLAIM_NUMBER)
    page.context.close()


@pytest.mark.e2e
def test_pagination_serves_fifty_rows_per_page(browser: Browser, bench_server: str) -> None:
    page = logged_in_page(browser, bench_server)
    page.goto("/results")
    body_rows = page.locator("table tbody tr")
    expect(body_rows).to_have_count(50)
    first_row = body_rows.first.inner_text()
    page.get_by_role("navigation", name="Pagination").get_by_role(
        "link", name="2", exact=True
    ).click()
    expect(page).to_have_url(re.compile(r"page=2"))
    expect(body_rows).to_have_count(50)
    assert body_rows.first.inner_text() != first_row
    page.context.close()


@pytest.mark.e2e
def test_detail_page_labels_every_value(browser: Browser, bench_server: str) -> None:
    page = logged_in_page(browser, bench_server)
    page.goto("/results")
    number = page.get_by_role("cell").first.inner_text().strip()
    page.get_by_role("link", name=number, exact=True).click()
    labels = [t.strip() for t in page.locator("dl.claim-detail dt").all_inner_texts()]
    values = [t.strip() for t in page.locator("dl.claim-detail dd").all_inner_texts()]
    assert labels == LABELS
    assert all(values) and values[0] == number
    page.context.close()


@pytest.mark.e2e
def test_export_csv_matches_the_results_page(browser: Browser, bench_server: str) -> None:
    page = logged_in_page(browser, bench_server)
    page.goto("/results")
    table = [
        [c.strip() for c in row.locator("td").all_inner_texts()]
        for row in page.locator("table tbody tr").all()
    ]
    response = page.request.get("/export.csv")
    assert response.ok and response.headers["content-type"].startswith("text/csv")
    exported = list(csv.reader(io.StringIO(response.text())))
    assert exported[0] == LABELS
    assert exported[1:51] == table
    page.context.close()
