"""The Capturable a live page offers. browser/ -> heallog/ is the permitted direction.

Built from the raw Playwright page, which only browser/ may hold: production code obtains it
with `LiveCapture.of(handle, dom)`, which goes through driver._playwright_page (the request
ledger must still be in scope) and takes the Sensitivity label from the handle's
AuthorisedTarget -- the scope record decides the label, never the caller (R3).

The screenshot's mask is computed against the LIVE page at screenshot time, never only from
the snapshot string (fix round 1, R4 / S-I1), and it fails closed:

1. Every mask id must match exactly one live element, or the capture is refused -- a
   Playwright mask locator that matches nothing silently masks nothing.
2. CLIENT_CONFIDENTIAL is default-deny for pixels: one bounded JS pass walks the document,
   every OPEN shadow root and every SAME-ORIGIN frame, and marks every input (except hidden),
   textarea, select and contenteditable. A CROSS-ORIGIN frame (and any object/embed) cannot be
   inspected, so the frame element is marked whole. A CLOSED shadow root is invisible to page
   script, so its host is found over CDP (DOM.getDocument with pierce) and marked whole.
   Marks are a per-capture nonce attribute, and the mask is the locator for that nonce in
   every frame -- so a node re-rendered or added after the snapshot is still masked.
3. After the screenshot the same pass re-checks the page: anything that needed a mask and
   lacks one (a control added mid-screenshot) refuses the capture rather than return it.
4. The pass has a node ceiling (LIVE_MAX_NODES) and every call a timeout; exceeding either
   raises CaptureError. As with a snapshot timeout, a page whose call timed out must not be
   reused: the timeout cancels only the Python await, not the work in the browser.

PUBLIC and SYNTHETIC captures keep the heuristic floor (step 1 only), per the R4 ruling.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Awaitable
from typing import Any

from bok_core.boundary.sensitivity import Sensitivity
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from selector_drift.browser.driver import PageHandle, _playwright_page
from selector_drift.browser.page_source import MAX_SNAPSHOT_NODES
from selector_drift.heallog.capture import NODE_ATTR, NODE_ID_RE, BBox, CaptureError

CALL_TIMEOUT_S = 30
# A bound on how many nodes one screenshot may mask by id; past it the capture is refused.
MAX_MASKED_NODES = 2_000
# Elements the live pass may walk (document + open shadow roots + same-origin frames).
LIVE_MAX_NODES = MAX_SNAPSHOT_NODES
MASK_ATTR = "data-sd-mask"

_BOX_JS = """(args) => {
  const el = document.querySelector(`[${args.attr}="${CSS.escape(args.id)}"]`);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return {x: r.x + window.scrollX, y: r.y + window.scrollY, width: r.width, height: r.height};
}"""

# mode: "mark" stamps the nonce; "verify" counts what needed a mark and lacks one;
# "clear" removes this capture's marks. One traversal for all three, so they cannot disagree.
_LIVE_PASS_JS = """({attr, maskAttr, nonce, ids, denyAll, max, mode}) => {
  const marked = (el) => el.getAttribute(maskAttr) === nonce;
  let unmasked = 0;
  const need = (el) => {
    if (mode === 'mark') el.setAttribute(maskAttr, nonce);
    else if (mode === 'verify' && !marked(el)) unmasked++;
  };
  if (mode !== 'clear') {
    for (const id of ids) {
      const hits = document.querySelectorAll(`[${attr}="${CSS.escape(id)}"]`);
      if (hits.length !== 1) return {refused: 'stale', count: hits.length};
      need(hits[0]);
    }
  }
  if (!denyAll && mode !== 'clear') return {seen: 0, unmasked};
  let seen = 0;
  const stack = [document];
  while (stack.length) {
    const root = stack.pop();
    const all = root.querySelectorAll('*');
    seen += all.length;
    if (seen > max) return {refused: 'too_large', count: seen};
    for (const el of all) {
      if (mode === 'clear') {
        if (marked(el)) el.removeAttribute(maskAttr);
      } else if (denyAll) {
        const tag = el.localName;
        if ((tag === 'input' && String(el.type).toLowerCase() !== 'hidden')
            || tag === 'textarea' || tag === 'select' || el.isContentEditable) need(el);
      }
      if (el.shadowRoot) stack.push(el.shadowRoot);
      const tag = el.localName;
      if (tag === 'iframe' || tag === 'frame' || tag === 'object' || tag === 'embed') {
        let doc = null;
        try { doc = (tag === 'iframe' || tag === 'frame') ? el.contentDocument : null; }
        catch (e) { doc = null; }
        if (doc && doc.documentElement) stack.push(doc);
        else if (mode !== 'clear' && denyAll) need(el);
      }
    }
  }
  return {seen, unmasked};
}"""


async def _bounded[T](call: Awaitable[T], what: str) -> T:
    try:
        return await asyncio.wait_for(call, CALL_TIMEOUT_S)
    except TimeoutError:
        raise CaptureError(f"{what} timed out after {CALL_TIMEOUT_S}s; refusing") from None


def _closed_hosts(document: dict[str, Any]) -> tuple[list[int], int]:
    """CDP node ids of every closed-shadow host, and how many elements were walked."""
    hosts: list[int] = []
    seen = 0
    stack: list[dict[str, Any]] = [document]
    while stack:
        node = stack.pop()
        if node.get("nodeType") == 1:
            seen += 1
        roots = node.get("shadowRoots") or []
        if any(r.get("shadowRootType") == "closed" for r in roots):
            hosts.append(int(node["nodeId"]))
        stack.extend(node.get("children") or [])
        stack.extend(roots)
        if node.get("contentDocument"):
            stack.append(node["contentDocument"])
    return hosts, seen


class LiveCapture:
    def __init__(self, page: Page, dom: str, *, sensitivity: Sensitivity) -> None:
        self._page = page
        self._dom = dom
        self._sensitivity = sensitivity

    @classmethod
    def of(cls, handle: PageHandle, dom: str) -> LiveCapture:
        """The production constructor: page and label both come from the authorised handle."""
        return cls(_playwright_page(handle), dom, sensitivity=handle._target.sensitivity)

    @property
    def viewport(self) -> tuple[int, int]:
        size = self._page.viewport_size
        return (size["width"], size["height"]) if size else (0, 0)

    @property
    def sensitivity(self) -> Sensitivity:
        return self._sensitivity

    def dom(self) -> str:
        return self._dom

    async def _pass(self, mode: str, ids: list[str], nonce: str) -> int:
        args = {
            "attr": NODE_ATTR,
            "maskAttr": MASK_ATTR,
            "nonce": nonce,
            "ids": ids,
            "denyAll": self._sensitivity is Sensitivity.CLIENT_CONFIDENTIAL,
            "max": LIVE_MAX_NODES,
            "mode": mode,
        }
        result: dict[str, Any] = await _bounded(
            self._page.evaluate(_LIVE_PASS_JS, args), "the live mask pass"
        )
        if result.get("refused") == "stale":
            raise CaptureError(
                f"a mask id matches {int(result['count'])} live elements, not exactly one: the "
                "snapshot is stale, so the screenshot could not be masked"
            )
        if result.get("refused") == "too_large":
            raise CaptureError(
                f"the live mask pass walked over its node ceiling of {LIVE_MAX_NODES}; refusing"
            )
        return int(result.get("unmasked", 0))

    async def _closed_shadow_hosts(self, nonce: str, *, mark: bool) -> int:
        """Mark (or, with mark=False, count unmarked) closed-shadow hosts over CDP. Chromium
        only -- a browser without CDP refuses here rather than leave closed roots unmasked."""
        try:
            cdp = await _bounded(self._page.context.new_cdp_session(self._page), "CDP attach")
        except PlaywrightError:
            raise CaptureError(
                "no CDP session: closed shadow roots cannot be found, refusing"
            ) from None
        try:
            doc = await _bounded(
                cdp.send("DOM.getDocument", {"depth": -1, "pierce": True}), "DOM.getDocument"
            )
            hosts, seen = _closed_hosts(doc["root"])
            if seen > LIVE_MAX_NODES * 2:
                raise CaptureError(
                    f"the closed-shadow pass walked over its node ceiling ({seen}); refusing"
                )
            unmarked = 0
            for host in hosts:
                if mark:
                    await _bounded(
                        cdp.send(
                            "DOM.setAttributeValue",
                            {"nodeId": host, "name": MASK_ATTR, "value": nonce},
                        ),
                        "DOM.setAttributeValue",
                    )
                else:
                    attrs = await _bounded(
                        cdp.send("DOM.getAttributes", {"nodeId": host}), "DOM.getAttributes"
                    )
                    flat = list(attrs.get("attributes", []))
                    pairs = dict(zip(flat[::2], flat[1::2], strict=False))
                    unmarked += pairs.get(MASK_ATTR) != nonce
            return unmarked
        finally:
            # Detaching is housekeeping; the capture's outcome is already decided.
            with contextlib.suppress(PlaywrightError, TimeoutError):
                await asyncio.wait_for(cdp.detach(), CALL_TIMEOUT_S)

    def _masks(self, nonce: str) -> list[Locator]:
        selector = f'[{MASK_ATTR}="{nonce}"]'  # CSS pierces open shadow roots
        return [frame.locator(selector) for frame in self._page.frames]

    async def masked_screenshot(self, *, mask_node_ids: frozenset[str]) -> bytes:
        for node_id in mask_node_ids:
            if not NODE_ID_RE.fullmatch(node_id):
                raise CaptureError("refusing to mask by an id that is not a minted node id")
        if len(mask_node_ids) > MAX_MASKED_NODES:
            raise CaptureError(
                f"{len(mask_node_ids)} nodes to mask exceeds {MAX_MASKED_NODES}; refusing"
            )
        deny_all = self._sensitivity is Sensitivity.CLIENT_CONFIDENTIAL
        ids = sorted(mask_node_ids)
        nonce = secrets.token_hex(8)
        try:
            await self._pass("mark", ids, nonce)
            if deny_all:
                await self._closed_shadow_hosts(nonce, mark=True)
            shot = await self._page.screenshot(
                full_page=True,
                type="png",
                mask=self._masks(nonce),
                mask_color="#000000",
                animations="disabled",
                caret="hide",
                timeout=CALL_TIMEOUT_S * 1000,
            )
            late = await self._pass("verify", ids, nonce)
            if deny_all:
                late += await self._closed_shadow_hosts(nonce, mark=False)
            if late:
                raise CaptureError(
                    f"{late} element(s) needing a mask appeared during the screenshot; refusing"
                )
            return shot
        finally:
            # Removing our marks is housekeeping; the outcome is already decided.
            with contextlib.suppress(CaptureError, PlaywrightError, TimeoutError):
                await asyncio.wait_for(self._pass("clear", ids, nonce), CALL_TIMEOUT_S)

    async def bbox(self, node_id: str) -> BBox | None:
        if not NODE_ID_RE.fullmatch(node_id):
            raise CaptureError("refusing to measure an id that is not a minted node id")
        rect = await _bounded(
            self._page.evaluate(_BOX_JS, {"attr": NODE_ATTR, "id": node_id}), "bbox"
        )
        return None if rect is None else BBox.model_validate(rect)
