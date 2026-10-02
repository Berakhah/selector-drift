"""The live page, snapshotted once (P1).

One evaluate() stamps data-sd-node on every element and asks the browser what is rendered
and what has a real box; the outerHTML is read in the same call and the accessibility tree
once after it. Every rung then runs in-process over that snapshot -- no CDP round-trip per
rung, per field, per row. The stamped ids stay on the live page, which is how LiveCapture
(Task 19) masks and measures the same nodes the rungs resolved.
"""

from __future__ import annotations

import re
from typing import Any

from lxml.html import HtmlElement
from playwright.async_api import Page

from selector_drift.browser.driver import PageHandle
from selector_drift.resolve.page import NODE_ATTR, ParsedPage

_LIVE_ID = re.compile(r"^n\d{1,7}$")

_SNAPSHOT_JS = """(attr) => {
  const all = Array.from(document.querySelectorAll('*'));
  all.forEach((el, i) => el.setAttribute(attr, 'n' + i));
  const clipMemo = new Map();
  const clips = (el) => {
    if (clipMemo.has(el)) return clipMemo.get(el);
    const s = getComputedStyle(el);
    const w = parseFloat(s.width), h = parseFloat(s.height);
    const v = s.clip === 'rect(0px, 0px, 0px, 0px)' || s.clipPath === 'inset(50%)'
      || ((w <= 1 || h <= 1) && s.overflow === 'hidden');
    clipMemo.set(el, v);
    return v;
  };
  const rendered = [], boxed = [];
  for (const el of all) {
    const id = el.getAttribute(attr);
    const shown = el.checkVisibility({checkVisibilityCSS: true, visibilityProperty: true})
      && el.closest('[aria-hidden="true"]') === null;
    if (shown) rendered.push(id);
    const r = el.getBoundingClientRect();
    const onPage = r.right + window.scrollX > 0 && r.bottom + window.scrollY > 0;
    let clipped = clips(el);
    for (let a = el.parentElement; a && !clipped; a = a.parentElement) clipped = clips(a);
    if (r.width > 1 && r.height > 1 && onPage && !clipped) boxed.push(id);
  }
  return {html: document.documentElement.outerHTML, rendered, boxed};
}"""


class LivePageSource(ParsedPage):
    def __init__(
        self, html: str, *, rendered: frozenset[str], boxed: frozenset[str], browser_ax: str
    ) -> None:
        super().__init__(html)
        self._rendered, self._boxed = rendered, boxed
        self.browser_ax = browser_ax  # Chromium's own tree, for parity checks and evidence

    @classmethod
    async def snapshot(cls, page: PageHandle) -> LivePageSource:
        return await cls.from_playwright(page.raw)

    @classmethod
    async def from_playwright(cls, page: Page) -> LivePageSource:
        raw: dict[str, Any] = await page.evaluate(_SNAPSHOT_JS, NODE_ATTR)
        browser_ax = await page.locator("html").aria_snapshot()
        return cls(
            str(raw["html"]),
            rendered=frozenset(map(str, raw["rendered"])),
            boxed=frozenset(map(str, raw["boxed"])),
            browser_ax=browser_ax,
        )

    def node_id(self, node: HtmlElement) -> str:
        ident = node.get(NODE_ATTR)
        if not ident:
            raise LookupError("element carries no data-sd-node: not part of this snapshot")
        return str(ident)

    def node(self, node_id: str) -> HtmlElement | None:
        if not _LIVE_ID.fullmatch(node_id):
            return None
        hits = self.root().xpath(f"//*[@{NODE_ATTR}=$n]", n=node_id)
        if isinstance(hits, list) and len(hits) == 1 and isinstance(hits[0], HtmlElement):
            return hits[0]
        return None

    def ax_hidden(self, node: HtmlElement) -> bool:
        return node.get(NODE_ATTR) not in self._rendered

    def boxed(self, node: HtmlElement) -> bool:
        return node.get(NODE_ATTR) in self._boxed
