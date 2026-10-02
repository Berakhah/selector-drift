"""Read a results page the way the tests need to, whatever markup a mutation left behind.

Header and data cells are found by element OR by ARIA role, so the same helpers read a <table> and
the role-annotated <div> grid TableToDiv (Task 9) produces. Text is the accessible text: aria-label,
then img alt, then text content -- which is what ImageHeader (Task 11A) needs.
"""

from __future__ import annotations

import datetime as dt
import random
import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

import lxml.html
from playwright.sync_api import Browser, Page
from starlette.testclient import TestClient

from testbench.mutations.base import CatalogueEntry, Mutation, MutationBase, parse, serialise
from testbench.portal.day import sign
from testbench.schedule import scheduled_names

FIXTURE_DAY = dt.date(2026, 9, 9)
CLAIM_NUMBER = re.compile(r"^[A-Z]{2}\d{8}$")
AMOUNT = re.compile(r"^\d{1,3}(,\d{3})*\.\d{2}$")


def root(html: str) -> lxml.html.HtmlElement:
    return lxml.html.document_fromstring(html)


def accessible_text(el: lxml.html.HtmlElement) -> str:
    if el.get("aria-hidden") == "true":
        return ""
    label = el.get("aria-label")
    if label:
        return label.strip()
    if el.tag == "img":
        return (el.get("alt") or "").strip()
    parts = [el.text or ""]
    for child in el:
        if isinstance(child.tag, str):
            parts.append(" " + accessible_text(child) + " ")
        parts.append(child.tail or "")
    return " ".join("".join(parts).split())


def _elements(html: str, xpath: str) -> list[lxml.html.HtmlElement]:
    return [e for e in root(html).xpath(xpath) if isinstance(e, lxml.html.HtmlElement)]


def headers(html: str) -> list[str]:
    return [accessible_text(h) for h in _elements(html, '//th | //*[@role="columnheader"]')]


def rows(html: str) -> list[list[str]]:
    out: list[list[str]] = []
    for row in _elements(html, '//tr | //*[@role="row"]'):
        found = [
            c
            for c in row.xpath('./td | ./*[@role="cell" or @role="gridcell"]')
            if isinstance(c, lxml.html.HtmlElement)
        ]
        if found:
            out.append([accessible_text(c) for c in found])
    return out


def cells(html: str) -> list[str]:
    return [value for row in rows(html) for value in row]


def column(html: str, label: str) -> list[str]:
    index = headers(html).index(label)
    return [row[index] for row in rows(html)]


def is_plausible_amount(value: str) -> bool:
    if not AMOUNT.fullmatch(value):
        return False
    try:
        amount = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return False
    return Decimal("100.00") <= amount <= Decimal("250000.00")


def normalise(html: str) -> str:
    return re.sub(r">\s+<", "><", html).strip()


def sign_override(day: dt.date, secret: str) -> dict[str, str]:
    text = day.isoformat()
    return {"X-Testbench-Date": text, "X-Testbench-Signature": sign(text, secret.encode("utf-8"))}


def login(client: TestClient) -> None:
    response = client.post("/login", data={"username": "demo", "password": "demo"})
    assert response.status_code == 303, response.text


def logged_in_page(
    browser: Browser, base: str, *, override_day: dt.date | None = None, secret: str | None = None
) -> Page:
    """Form fields are addressed by `name`, which no mutation may rename: a real portal's UI
    change never breaks its own login form."""
    context = browser.new_context(base_url=base)
    if override_day is not None:
        if secret is None:
            raise ValueError("a date override needs the override secret")
        context.set_extra_http_headers(sign_override(override_day, secret))
    page = context.new_page()
    page.goto("/login")
    page.locator("input[name=username]").fill("demo")
    page.locator("input[name=password]").fill("demo")
    page.locator("form button[type=submit]").click()
    page.wait_for_url("**/search")
    return page


class TagBody(MutationBase):
    """A test-only mutation: marks <body> with one attribute. Visible, harmless, and distinct
    per instance, so schedules can be told apart."""

    def __init__(self, tag: str) -> None:
        self._tag = tag

    @property
    def name(self) -> str:
        return f"TagBody{self._tag}"

    def apply(self, html: str) -> str:
        doc = parse(html)
        body = doc.find("body")
        assert body is not None
        body.set(f"data-test-{self._tag.lower()}", "1")
        return serialise(doc)


class WrapTable(MutationBase):
    """A test-only structural mutation: wraps every <table> in a <div>, moving every cell's path."""

    def apply(self, html: str) -> str:
        doc = parse(html)
        for table in doc.xpath("//table"):
            parent = table.getparent()
            assert parent is not None
            wrapper = doc.makeelement("div", {"class": "sd-test-wrap"})
            parent.replace(table, wrapper)
            wrapper.append(table)
        return serialise(doc)


def _builder(tag: str) -> Callable[[random.Random], Mutation]:
    def build(rng: random.Random) -> Mutation:
        return TagBody(tag)

    return build


def bench_catalogue() -> tuple[CatalogueEntry, ...]:
    benign = [CatalogueEntry(f"TagBody{t}", _builder(t), adversarial=False) for t in "ABCDEF"]
    hostile = [CatalogueEntry(f"TagBody{t}", _builder(t), adversarial=True) for t in "XY"]
    return (*benign, *hostile)


WRAP_TABLE = (CatalogueEntry("WrapTable", WrapTable.from_rng, adversarial=False),)


def first_day_scheduling(
    name: str, start: dt.date, *, target: str = "testbench", horizon: int = 3650
) -> dt.date:
    for offset in range(horizon):
        day = start + dt.timedelta(days=offset)
        if name in scheduled_names(day, target):
            return day
    raise LookupError(f"{name} is never scheduled within {horizon} days of {start}")


def render_in_browser(browser: Browser, html: str) -> str:
    """The DOM a browser builds from `html` after load -- scripts run, exactly as on the bench."""
    context = browser.new_context()
    page = context.new_page()
    page.set_content(html, wait_until="load")
    dom = str(page.evaluate("document.documentElement.outerHTML"))
    context.close()
    return dom
