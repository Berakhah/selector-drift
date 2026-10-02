"""The Capturable a live page offers. browser/ -> heallog/ is the permitted direction.

Built from the raw Playwright page, which only browser/ may hold: obtain it with
`LiveCapture.of(handle, dom)` (goes through driver._playwright_page, which checks the request
ledger is still in scope) rather than passing a page in from outside.

Every browser call is bounded (the LivePageSource discipline, S-I2): the box lookup is one
querySelector -- never a DOM walk -- under CALL_TIMEOUT_S, and the screenshot carries
Playwright's own timeout. As with a snapshot timeout, a page whose call timed out must not
be reused: the timeout cancels only the Python await, not the work in the browser.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

from selector_drift.browser.driver import PageHandle, _playwright_page
from selector_drift.heallog.capture import NODE_ATTR, NODE_ID_RE, BBox, CaptureError

CALL_TIMEOUT_S = 30
# A bound on how many nodes one screenshot may mask; past it the capture is refused, not cut.
MAX_MASKED_NODES = 2_000

_BOX_JS = """(args) => {
  const el = document.querySelector(`[${args.attr}="${CSS.escape(args.id)}"]`);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return {x: r.x + window.scrollX, y: r.y + window.scrollY, width: r.width, height: r.height};
}"""


class LiveCapture:
    def __init__(self, page: Page, dom: str) -> None:
        self._page = page
        self._dom = dom

    @classmethod
    def of(cls, handle: PageHandle, dom: str) -> LiveCapture:
        return cls(_playwright_page(handle), dom)

    @property
    def viewport(self) -> tuple[int, int]:
        size = self._page.viewport_size
        return (size["width"], size["height"]) if size else (0, 0)

    def dom(self) -> str:
        return self._dom

    async def screenshot(self, *, mask_node_ids: frozenset[str]) -> bytes:
        for node_id in mask_node_ids:
            if not NODE_ID_RE.fullmatch(node_id):
                raise CaptureError("refusing to mask by an id that is not a minted node id")
        if len(mask_node_ids) > MAX_MASKED_NODES:
            raise CaptureError(
                f"{len(mask_node_ids)} nodes to mask exceeds {MAX_MASKED_NODES}; refusing"
            )
        masks = [self._page.locator(f'[{NODE_ATTR}="{n}"]') for n in sorted(mask_node_ids)]
        return await self._page.screenshot(
            full_page=True,
            type="png",
            mask=masks,
            mask_color="#000000",
            animations="disabled",
            caret="hide",
            timeout=CALL_TIMEOUT_S * 1000,
        )

    async def bbox(self, node_id: str) -> BBox | None:
        if not NODE_ID_RE.fullmatch(node_id):
            raise CaptureError("refusing to measure an id that is not a minted node id")
        rect = await asyncio.wait_for(
            self._page.evaluate(_BOX_JS, {"attr": NODE_ATTR, "id": node_id}), CALL_TIMEOUT_S
        )
        return None if rect is None else BBox.model_validate(rect)
