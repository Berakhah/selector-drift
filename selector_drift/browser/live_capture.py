"""The Capturable a live page offers. browser/ -> heallog/ is the permitted direction.

Built from the raw Playwright page, which only browser/ may hold: production code obtains it
with `LiveCapture.of(handle, dom)`, which goes through driver._playwright_page (the request
ledger must still be in scope) and takes the Sensitivity label from the handle's
AuthorisedTarget -- the scope record decides the label, never the caller (R3).

The screenshot's mask is computed against the LIVE page at screenshot time, never only from
the snapshot string (fix round 1, R4 / S-I1), and it fails closed:

1. Every mask id must match exactly one live element, or the capture is refused -- a
   Playwright mask locator that matches nothing silently masks nothing.
2. CLIENT_CONFIDENTIAL is default-deny for pixels, and the classification runs over CDP, never
   in the page's main JS world (fix round 2, N-M1: a page script that patches
   Element.prototype.shadowRoot or querySelectorAll cannot hide anything from it).
   DOM.getDocument with pierce gives the whole tree -- open AND closed shadow roots and
   same-process frames. Marked: every input (except hidden), textarea, select and
   [contenteditable]; every CLOSED shadow host whole (Playwright's locators cannot reach
   inside a closed root); every frame whose document is cross-origin or absent, and every
   object/embed, whole. Open shadow roots and same-origin frames are descended.
3. Marks are a per-capture nonce attribute set with DOM.setAttributeValue. The mask is the
   nonce locator in every frame, and before the screenshot the locators must find exactly as
   many elements as were marked -- if Playwright cannot see a mark, it could not mask it.
4. After the screenshot the same CDP classification re-reads the page: anything that needed
   a mask and lacks one (added mid-screenshot) refuses the capture.
5. Bounded: the light-DOM element count is checked over CDP BEFORE the pierced tree is
   transferred (LIVE_MAX_NODES), the pierced tree is held to 2x that, marks to
   MAX_LIVE_MARKS, and every call to CALL_TIMEOUT_S. Every CDP failure, timeout or malformed
   reply is a CaptureError (fix round 2, N-M2) -- a refusal, never a retryable Playwright
   error. As with a snapshot timeout, a page whose call timed out must not be reused.

PUBLIC and SYNTHETIC captures keep the heuristic floor (step 1 and the step-3 count only),
per the R4 ruling. Chromium only: without CDP a CLIENT_CONFIDENTIAL capture refuses.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections import Counter
from collections.abc import Awaitable
from typing import Any
from urllib.parse import urlsplit

from bok_core.boundary.sensitivity import Sensitivity
from playwright.async_api import CDPSession, Locator, Page
from playwright.async_api import Error as PlaywrightError

from selector_drift.browser.driver import PageHandle, _playwright_page
from selector_drift.browser.page_source import MAX_SNAPSHOT_NODES
from selector_drift.heallog.capture import NODE_ATTR, NODE_ID_RE, BBox, CaptureError, raised

CALL_TIMEOUT_S = 30
# A bound on how many nodes one screenshot may mask by id; past it the capture is refused.
MAX_MASKED_NODES = 2_000
# Light-DOM elements the live pass accepts before transferring the pierced tree.
LIVE_MAX_NODES = MAX_SNAPSHOT_NODES
# Elements one capture may mark (form controls + whole-masked containers + ids).
MAX_LIVE_MARKS = 10_000
MASK_ATTR = "data-sd-mask"

_BOX_JS = """(args) => {
  const el = document.querySelector(`[${args.attr}="${CSS.escape(args.id)}"]`);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return {x: r.x + window.scrollX, y: r.y + window.scrollY, width: r.width, height: r.height};
}"""

# PUBLIC/SYNTHETIC only (the heuristic floor): the snapshot's mask ids, marked by nonce.
_ID_PASS_JS = """({attr, maskAttr, nonce, ids, mode}) => {
  if (mode === 'clear') {
    for (const el of document.querySelectorAll(`[${maskAttr}="${nonce}"]`))
      el.removeAttribute(maskAttr);
    return {unmasked: 0};
  }
  let unmasked = 0;
  for (const id of ids) {
    const hits = document.querySelectorAll(`[${attr}="${CSS.escape(id)}"]`);
    if (hits.length !== 1) return {refused: 'stale', count: hits.length};
    if (mode === 'mark') hits[0].setAttribute(maskAttr, nonce);
    else if (hits[0].getAttribute(maskAttr) !== nonce) unmasked++;
  }
  return {unmasked};
}"""

_CONTROL_TAGS = frozenset({"textarea", "select"})
_WHOLE_TAGS = frozenset({"object", "embed"})
_FRAME_TAGS = frozenset({"iframe", "frame"})


async def _bounded[T](call: Awaitable[T], what: str) -> T:
    try:
        return await asyncio.wait_for(call, CALL_TIMEOUT_S)
    except TimeoutError:
        raise CaptureError(f"{what} timed out after {CALL_TIMEOUT_S}s; refusing") from None


def _stale(count: int) -> CaptureError:
    return CaptureError(
        f"a mask id matches {count} live elements, not exactly one: the snapshot is stale, "
        "so the screenshot could not be masked"
    )


def _origin(url: str | None, parent: str) -> str:
    """about:blank / about:srcdoc inherit their parent's origin; anything else is its own."""
    if not url or url.startswith("about:"):
        return parent
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _attrs(node: dict[str, Any]) -> dict[str, str]:
    flat = node.get("attributes") or []
    return {str(k): str(v) for k, v in zip(flat[::2], flat[1::2], strict=False)}


def _needs_mask(node: dict[str, Any], ids: frozenset[str], id_hits: Counter[str]) -> bool:
    """One element's verdict, from CDP's view of it (no page script involved)."""
    attrs = _attrs(node)
    tag = str(node.get("localName") or node.get("nodeName", "")).lower()
    hit = attrs.get(NODE_ATTR)
    by_id = hit is not None and hit in ids
    if by_id and hit is not None:
        id_hits[hit] += 1
    editable = "contenteditable" in attrs and attrs["contenteditable"].lower() != "false"
    control = (tag == "input" and attrs.get("type", "text").lower() != "hidden") or (
        tag in _CONTROL_TAGS
    )
    return by_id or control or editable or tag in _WHOLE_TAGS


def _classify(
    root: dict[str, Any], ids: frozenset[str]
) -> tuple[list[dict[str, Any]], Counter[str], int]:
    """Every element node that must be masked, the id hit counts, and elements walked."""
    marks: list[dict[str, Any]] = []
    id_hits: Counter[str] = Counter()
    seen = 0
    top = _origin(root.get("documentURL"), "about:")
    stack: list[tuple[dict[str, Any], str]] = [(root, top)]
    while stack:
        node, origin = stack.pop()
        children = list(node.get("children") or [])
        if node.get("nodeType") == 1:
            seen += 1
            if seen > LIVE_MAX_NODES * 2:
                raise CaptureError(
                    f"the live mask pass walked over its node ceiling ({seen}); refusing"
                )
            tag = str(node.get("localName") or node.get("nodeName", "")).lower()
            whole = False
            for shadow in node.get("shadowRoots") or []:
                kind = shadow.get("shadowRootType")
                if kind == "closed":
                    whole = True  # locators cannot reach inside: mask the host
                elif kind == "open":
                    stack.append((shadow, origin))
            if tag in _FRAME_TAGS:
                inner = node.get("contentDocument")
                inner_origin = (
                    _origin(inner.get("documentURL"), origin) if inner is not None else None
                )
                if inner is None or inner_origin != origin:
                    whole = True  # cross-origin or out-of-process: cannot be inspected
                else:
                    stack.append((inner, origin))
            if whole or _needs_mask(node, ids, id_hits):
                marks.append(node)
        stack.extend((child, origin) for child in children)
    return marks, id_hits, seen


class _Cdp:
    """One CDP session; every failure, timeout and malformed reply is a CaptureError."""

    def __init__(self, session: CDPSession) -> None:
        self._session = session

    async def send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            reply = await _bounded(self._session.send(method, params), method)
        except PlaywrightError:
            raise CaptureError(f"CDP {method} failed; refusing") from None
        if not isinstance(reply, dict):
            raise CaptureError(f"CDP {method} returned a malformed reply; refusing")
        return reply

    async def tree(self) -> dict[str, Any]:
        """The pierced document -- after the light-DOM count is checked, so an oversized page
        is refused before its whole tree is transferred."""
        try:
            shallow = await self.send("DOM.getDocument", {"depth": 0})
            root_id = int(shallow["root"]["nodeId"])
            found = await self.send("DOM.querySelectorAll", {"nodeId": root_id, "selector": "*"})
            count = len(found["nodeIds"])
        except (KeyError, TypeError, ValueError):
            raise CaptureError("CDP returned a malformed document; refusing") from None
        if count > LIVE_MAX_NODES:
            raise CaptureError(
                f"the page has {count} elements, over the live mask pass's node ceiling of "
                f"{LIVE_MAX_NODES}; refusing"
            )
        full = await self.send("DOM.getDocument", {"depth": -1, "pierce": True})
        root = full.get("root")
        if not isinstance(root, dict):
            raise CaptureError("CDP returned a malformed document; refusing")
        return root

    async def detach(self) -> None:
        with contextlib.suppress(PlaywrightError, TimeoutError):
            await asyncio.wait_for(self._session.detach(), CALL_TIMEOUT_S)


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

    # -- PUBLIC / SYNTHETIC: mask ids only ---------------------------------------------------
    async def _id_pass(self, mode: str, ids: list[str], nonce: str) -> int:
        args = {"attr": NODE_ATTR, "maskAttr": MASK_ATTR, "nonce": nonce, "ids": ids, "mode": mode}
        result: dict[str, Any] = await _bounded(
            self._page.evaluate(_ID_PASS_JS, args), "the live mask pass"
        )
        if result.get("refused") == "stale":
            raise _stale(int(result["count"]))
        return int(result.get("unmasked", 0))

    # -- CLIENT_CONFIDENTIAL: default-deny, classified over CDP --------------------------------
    async def _cdp(self) -> _Cdp:
        try:
            session = await _bounded(self._page.context.new_cdp_session(self._page), "CDP attach")
        except PlaywrightError:
            raise CaptureError("no CDP session: the page cannot be classified, refusing") from None
        return _Cdp(session)

    @staticmethod
    def _check_ids(ids: frozenset[str], id_hits: Counter[str]) -> None:
        for node_id in ids:
            if id_hits[node_id] != 1:
                raise _stale(id_hits[node_id])

    async def _mark_cc(self, cdp: _Cdp, ids: frozenset[str], nonce: str) -> int:
        marks, id_hits, _ = _classify(await cdp.tree(), ids)
        self._check_ids(ids, id_hits)
        if len(marks) > MAX_LIVE_MARKS:
            raise CaptureError(f"{len(marks)} elements to mask exceeds {MAX_LIVE_MARKS}; refusing")
        for node in marks:
            try:
                node_id = int(node["nodeId"])
            except (KeyError, TypeError, ValueError):
                raise CaptureError("CDP returned a malformed node; refusing") from None
            await cdp.send(
                "DOM.setAttributeValue", {"nodeId": node_id, "name": MASK_ATTR, "value": nonce}
            )
        return len(marks)

    async def _unmarked_cc(self, cdp: _Cdp, ids: frozenset[str], nonce: str) -> int:
        marks, id_hits, _ = _classify(await cdp.tree(), ids)
        self._check_ids(ids, id_hits)
        return sum(1 for node in marks if _attrs(node).get(MASK_ATTR) != nonce)

    async def _clear_cc(self, cdp: _Cdp, nonce: str) -> None:
        stack = [await cdp.tree()]
        while stack:
            node = stack.pop()
            if _attrs(node).get(MASK_ATTR) == nonce:
                await cdp.send(
                    "DOM.removeAttribute", {"nodeId": int(node["nodeId"]), "name": MASK_ATTR}
                )
            stack.extend(node.get("children") or [])
            stack.extend(node.get("shadowRoots") or [])
            if node.get("contentDocument"):
                stack.append(node["contentDocument"])

    # -- the screenshot ----------------------------------------------------------------------
    def _masks(self, nonce: str) -> list[Locator]:
        selector = f'[{MASK_ATTR}="{nonce}"]'  # CSS pierces open shadow roots
        return [frame.locator(selector) for frame in self._page.frames]

    async def _visible_marks(self, nonce: str) -> int:
        total = 0
        for locator in self._masks(nonce):
            try:
                total += await _bounded(locator.count(), "mask locator count")
            except PlaywrightError:
                raise CaptureError("a mask locator could not be evaluated; refusing") from None
        return total

    async def masked_screenshot(
        self, *, mask_node_ids: frozenset[str], sensitivity: Sensitivity | None = None
    ) -> bytes:
        """`sensitivity` may raise this page's label (never lower it); default-deny applies
        whenever the effective label is CLIENT_CONFIDENTIAL."""
        for node_id in mask_node_ids:
            if not NODE_ID_RE.fullmatch(node_id):
                raise CaptureError("refusing to mask by an id that is not a minted node id")
        if len(mask_node_ids) > MAX_MASKED_NODES:
            raise CaptureError(
                f"{len(mask_node_ids)} nodes to mask exceeds {MAX_MASKED_NODES}; refusing"
            )
        deny_all = raised(self._sensitivity, sensitivity) is Sensitivity.CLIENT_CONFIDENTIAL
        ids = sorted(mask_node_ids)
        nonce = secrets.token_hex(8)
        cdp = await self._cdp() if deny_all else None
        try:
            if cdp is not None:
                marked = await self._mark_cc(cdp, mask_node_ids, nonce)
            else:
                await self._id_pass("mark", ids, nonce)
                marked = len(ids)
            visible = await self._visible_marks(nonce)
            if visible != marked:
                raise CaptureError(
                    f"{marked} elements were marked but the mask can see {visible}; refusing"
                )
            try:
                shot = await self._page.screenshot(
                    full_page=True,
                    type="png",
                    mask=self._masks(nonce),
                    mask_color="#000000",
                    animations="disabled",
                    caret="hide",
                    timeout=CALL_TIMEOUT_S * 1000,
                )
            except PlaywrightError:
                raise CaptureError("the masked screenshot failed; refusing") from None
            if cdp is not None:
                late = await self._unmarked_cc(cdp, mask_node_ids, nonce)
            else:
                late = await self._id_pass("verify", ids, nonce)
            if late:
                raise CaptureError(
                    f"{late} element(s) needing a mask appeared during the screenshot; refusing"
                )
            return shot
        finally:
            # Removing our marks is housekeeping; the outcome is already decided.
            with contextlib.suppress(CaptureError, PlaywrightError, TimeoutError, KeyError):
                if cdp is not None:
                    await asyncio.wait_for(self._clear_cc(cdp, nonce), CALL_TIMEOUT_S)
                else:
                    await asyncio.wait_for(self._id_pass("clear", ids, nonce), CALL_TIMEOUT_S)
            if cdp is not None:
                await cdp.detach()

    async def bbox(self, node_id: str) -> BBox | None:
        if not NODE_ID_RE.fullmatch(node_id):
            raise CaptureError("refusing to measure an id that is not a minted node id")
        rect = await _bounded(
            self._page.evaluate(_BOX_JS, {"attr": NODE_ATTR, "id": node_id}), "bbox"
        )
        return None if rect is None else BBox.model_validate(rect)
