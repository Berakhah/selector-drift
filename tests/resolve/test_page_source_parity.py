"""R9. If the two PageSources disagree, replay is theatre and every mutation test is testing
something the log never saw. Browser-backed, so e2e; the unit tier never needs a browser."""

from __future__ import annotations

import asyncio
import threading

import pytest
from lxml.html import HtmlElement
from playwright.async_api import async_playwright

from selector_drift.browser.page_source import LivePageSource
from selector_drift.resolve.cascade import REGISTERED_RUNGS
from selector_drift.resolve.page import PageSource, StaticPageSource
from selector_drift.resolve.trace import ResolvedElement
from testbench.mutations.adversarial.hidden_decoy import HiddenDecoy
from tests.resolve.conftest import AMOUNT, CLAIM_NUMBER, BenchPage
from tests.resolve.test_page import SEMANTICS

PAGES = [
    "ClassRename",
    "DomRestructure",
    "ColumnReorder",
    "TableToDiv",
    "LabelReword",
    "IdChurn",
    "FrameworkMarkup",
    "Pagination",
    "DecoyColumn",
    "HiddenDecoy",
    "DuplicateLabel",
    "ImageHeader",
]


async def _live(html: str) -> LivePageSource:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.set_content(html)
        source = await LivePageSource.from_playwright(page)
        await browser.close()
        return source


def _snap(html: str) -> LivePageSource:
    """Run `_live` in its own thread's event loop (Task 12's pattern, test_runner_identity.py).

    pytest-playwright's sync API pins a running loop on the main thread for the session, so a
    plain asyncio.run() here fails whenever the sync e2e tests ran first in the same session."""
    result: list[LivePageSource] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(_live(html)))
        except BaseException as exc:  # re-raised on the test's own thread below
            error.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=120)
    if thread.is_alive():
        raise TimeoutError("the live snapshot did not finish within 120 s")
    if error:
        raise error[0]
    return result[0]


def _identity(page: PageSource, el: ResolvedElement | None) -> tuple[str, str] | None:
    """Node ids differ by construction (getpath vs data-sd-node); what must agree is WHICH
    cell: its truth mark and its text."""
    if el is None:
        return None
    node = page.node(el.node_id)
    assert isinstance(node, HtmlElement)
    return node.get("data-sd-truth") or "", " ".join(node.text_content().split())


@pytest.mark.e2e
@pytest.mark.parametrize("name", ["unmutated", *PAGES])
def test_live_and_static_sources_resolve_identically(name: str, bench: BenchPage) -> None:
    if name == "HiddenDecoy":
        # Parity rule (2026-09-26): a static source cannot see layout, so parity is asserted on
        # the variant both sources judge from markup alone. The display:none and off-screen
        # variants are covered per source by Task 14's decoy tests.
        page = bench.mutated_by(HiddenDecoy(variant="aria-hidden"))
    else:
        page = bench if name == "unmutated" else bench.mutated(name)
    live, static = _snap(page.marked), StaticPageSource(page.marked)
    for rung in [r for r in REGISTERED_RUNGS if r.rung_id in (2, 3, 4)]:
        for field in (CLAIM_NUMBER, AMOUNT):
            assert _identity(live, rung.resolve(live, field)) == _identity(
                static, rung.resolve(static, field)
            ), (name, rung.rung_id, field.name)


@pytest.mark.e2e
def test_visibility_agrees_with_the_browser() -> None:
    live, static = _snap(SEMANTICS), StaticPageSource(SEMANTICS)
    for probe in static.query("[data-probe]"):
        [twin] = live.query(f'[data-probe="{probe.get("data-probe")}"]')
        assert live.visible(twin) is static.visible(probe), probe.get("data-probe")


@pytest.mark.e2e
def test_our_header_names_are_chromiums(bench: BenchPage) -> None:
    """The computed names are ours (aria.py); this checks them against Chromium's tree."""
    live = _snap(bench.html)
    for node in live.ax().walk():
        if node.role == "columnheader":
            assert f'columnheader "{node.name}"' in live.browser_ax, node.name
