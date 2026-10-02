"""Pagination: the numbered page list is replaced by another pager style.

Portals swap pagers constantly: Previous/Next with a page count, a page-number <select>, or a
single "show more" link. The grid and its values are untouched; what breaks is any run step that
navigates by the old numbered links.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Self
from urllib.parse import parse_qsl, urlencode, urlsplit

from lxml.html import HtmlElement

from testbench.mutations.base import MutationBase, parse, serialise
from testbench.mutations.grid import detach, elements, grids

STYLES = ("prev-next", "select", "load-more")
PLACEMENTS = ("after", "before")


@dataclass(frozen=True, slots=True)
class _Pager:
    current: int
    pages: int
    base_query: tuple[tuple[str, str], ...]  # the search, without `page`

    def href(self, page: int) -> str:
        return "/results?" + urlencode([*self.base_query, ("page", str(page))])


def _read(nav: HtmlElement) -> _Pager | None:
    """Recover the pager's state from the numbered list Task 7 renders."""
    current: int | None = None
    pages = 0
    base: tuple[tuple[str, str], ...] = ()
    for item in elements(nav.xpath(".//li")):
        text = item.text_content().strip()
        if not text.isdigit():
            return None
        pages = max(pages, int(text))
        if elements(item.xpath('.//*[@aria-current="page"]')):
            current = int(text)
        for link in elements(item.xpath(".//a[@href]")):
            query = parse_qsl(urlsplit(str(link.get("href"))).query, keep_blank_values=True)
            base = tuple((k, v) for k, v in query if k != "page")
    if current is None or pages == 0:
        return None
    return _Pager(current, pages, base)


def _link(parent: HtmlElement, href: str, text: str, rel: str | None = None) -> HtmlElement:
    attrs = {"href": href} if rel is None else {"href": href, "rel": rel}
    link = parent.makeelement("a", attrs)
    link.text = text
    parent.append(link)
    return link


@dataclass(frozen=True, slots=True)
class Pagination(MutationBase):
    style: str = "prev-next"
    placement: str = "after"

    def __post_init__(self) -> None:
        if self.style not in STYLES:
            raise ValueError(f"unknown pager style {self.style!r}")
        if self.placement not in PLACEMENTS:
            raise ValueError(f"unknown placement {self.placement!r}")

    @classmethod
    def from_rng(cls, rng: random.Random) -> Self:
        return cls(style=rng.choice(STYLES), placement=rng.choice(PLACEMENTS))

    def _build(self, doc: HtmlElement, pager: _Pager) -> HtmlElement:
        if self.style == "prev-next":
            nav = doc.makeelement("nav", {"class": "pager", "aria-label": "Results pages"})
            if pager.current > 1:
                _link(nav, pager.href(pager.current - 1), "« Previous", "prev")
            status = nav.makeelement("span", {"class": "pager-status"})
            status.text = f"Page {pager.current} of {pager.pages}"
            nav.append(status)
            if pager.current < pager.pages:
                _link(nav, pager.href(pager.current + 1), "Next »", "next")
            return nav
        if self.style == "select":
            form = doc.makeelement(
                "form",
                {
                    "class": "pager",
                    "method": "get",
                    "action": "/results",
                    "aria-label": "Results pages",
                },
            )
            for key, value in pager.base_query:
                form.append(
                    form.makeelement("input", {"type": "hidden", "name": key, "value": value})
                )
            label = form.makeelement("label", {})
            label.text = "Page "
            select = label.makeelement("select", {"name": "page"})
            for number in range(1, pager.pages + 1):
                attrs = {"value": str(number)}
                if number == pager.current:
                    attrs["selected"] = "selected"
                option = select.makeelement("option", attrs)
                option.text = str(number)
                select.append(option)
            label.append(select)
            form.append(label)
            button = form.makeelement("button", {"type": "submit"})
            button.text = "Go"
            form.append(button)
            return form
        more = doc.makeelement("p", {"class": "load-more"})
        if pager.current < pager.pages:
            _link(more, pager.href(pager.current + 1), "Show more results", "next")
        else:
            more.text = "No more results"
        return more

    def apply(self, html: str) -> str:
        doc = parse(html)
        for nav in elements(doc.xpath('//nav[@aria-label="Pagination"]')):
            pager = _read(nav)
            parent = nav.getparent()
            if pager is None or parent is None:
                continue
            replacement = self._build(doc, pager)
            replacement.tail = nav.tail
            parent.replace(nav, replacement)
            tables = grids(doc)
            if self.placement == "before" and tables:
                detach(replacement)
                replacement.tail = None
                tables[0].addprevious(replacement)
        return serialise(doc)
