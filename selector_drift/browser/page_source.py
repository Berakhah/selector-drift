"""The live page, snapshotted once (P1).

One evaluate() stamps data-sd-node on every element and asks the browser what is rendered
and what has a real box; the outerHTML is read in the same call and the accessibility tree
once after it. Every rung then runs in-process over that snapshot -- no CDP round-trip per
rung, per field, per row. The stamped ids stay on the live page, which is how LiveCapture
(Task 19) masks and measures the same nodes the rungs resolved.

The snapshot is bounded (fix round 1, S-I2): the evaluate() is abandoned after
SNAPSHOT_TIMEOUT_S, and a page with more than MAX_SNAPSHOT_NODES elements is refused with
SnapshotTooLarge before anything is stamped or measured -- never silently truncated, because a
partial snapshot would let a rung resolve against a page it only half saw.

dom()/browser_ax are unredacted; redaction happens at the evidence-storage boundary (Task 33)
and nothing here may be persisted directly.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from lxml.html import HtmlElement
from playwright.async_api import Page

from selector_drift.browser.driver import PageHandle, _playwright_page
from selector_drift.resolve.page import NODE_ATTR, ParsedPage

_LIVE_ID = re.compile(r"^n\d{1,7}$")
SNAPSHOT_TIMEOUT_S = 30
MAX_SNAPSHOT_NODES = 50_000
_REFUSED = "too_large"

_SNAPSHOT_JS = """({attr, max}) => {
  const count = document.querySelectorAll('*').length;
  if (count > max) return {refused: 'too_large', count};
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


class SnapshotTooLarge(RuntimeError):
    """The page has more elements than MAX_SNAPSHOT_NODES. Refused, not truncated."""

    def __init__(self, count: int) -> None:
        super().__init__(
            f"page has {count} elements, over the {MAX_SNAPSHOT_NODES} snapshot ceiling; "
            "refusing rather than resolving against a partial snapshot"
        )
        self.count = count


class LivePageSource(ParsedPage):
    def __init__(
        self, html: str, *, rendered: frozenset[str], boxed: frozenset[str], browser_ax: str
    ) -> None:
        super().__init__(html)
        self._rendered, self._boxed = rendered, boxed
        self.browser_ax = browser_ax  # Chromium's own tree, for parity checks and evidence

    @classmethod
    async def snapshot(cls, page: PageHandle) -> LivePageSource:
        return await cls.from_playwright(_playwright_page(page))

    @classmethod
    async def from_playwright(cls, page: Page) -> LivePageSource:
        """Raises SnapshotTooLarge past the node ceiling, TimeoutError past the time limit."""
        limits = {"attr": NODE_ATTR, "max": MAX_SNAPSHOT_NODES}
        raw: dict[str, Any] = await asyncio.wait_for(
            page.evaluate(_SNAPSHOT_JS, limits), SNAPSHOT_TIMEOUT_S
        )
        if raw.get("refused") == _REFUSED:
            raise SnapshotTooLarge(int(raw["count"]))
        browser_ax = await page.locator("html").aria_snapshot(timeout=SNAPSHOT_TIMEOUT_S * 1000)
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
