from __future__ import annotations

import datetime as dt

import pytest
from playwright.sync_api import Browser

from testbench.mutations.catalogue import CATALOGUE
from testbench.portal.hook import mutate_html
from testbench.portal.render import render_results
from tests.testbench.helpers import (
    first_day_scheduling,
    logged_in_page,
    normalise,
    render_in_browser,
)


def _fixed_days() -> list[dt.date]:
    days = [dt.date(2026, 9, 9), dt.date(2026, 9, 10)]
    if any(e.name == "VirtualisedRows" for e in CATALOGUE):  # from Task 11A on
        days.append(first_day_scheduling("VirtualisedRows", start=dt.date(2026, 9, 1)))
    return days


FIXED_DAYS = _fixed_days()


@pytest.mark.e2e
@pytest.mark.parametrize("day", FIXED_DAYS, ids=str)
def test_the_page_a_browser_sees_is_what_the_engine_produced(
    browser: Browser, mutating_bench_server: str, override_secret: str, day: dt.date
) -> None:
    """The one test that guards the whole bench (bench spec §6.1). Both sides go through a real
    browser so VirtualisedRows' script runs on both; the expected side calls the same
    mutate_html the hook calls -- apply_all plus the seed tag the browser also sees."""
    page = logged_in_page(browser, mutating_bench_server, override_day=day, secret=override_secret)
    page.goto(f"{mutating_bench_server}/results")
    seen = normalise(str(page.evaluate("document.documentElement.outerHTML")))
    page.context.close()
    expected = normalise(render_in_browser(browser, mutate_html(day, render_results(day))))
    assert seen == expected
