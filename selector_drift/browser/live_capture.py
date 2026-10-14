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

6. The PIXELS are verified after the shot (fix round 3): Playwright paints its mask as an
   in-page overlay element, which page script or a stylesheet can remove or hide. Before the
   shot every marked node's content quads are read over CDP (DOM.getContentQuads, offset by
   Page.getLayoutMetrics into full-page coordinates); after it the PNG is decoded (stdlib,
   browser/png.py) and every quad's bounding box, inset by VERIFY_INSET_PX, must be entirely
   #000000 -- otherwise the capture is refused. A node with no layout box (display:none, a
   hidden input), a box no larger than the inset, or a box wholly off the page has nothing to
   verify: it painted nothing there. A geometry mismatch (an element that moves during the
   full-page capture) can only cause a false refusal, never a pass with colour in the box.
   This applies to every CLIENT_CONFIDENTIAL capture and to any capture with a non-empty mask.
   Verification is the control; Playwright's mask is not replaced by self-painting.
7. Marking and clearing are one CDP call per node, so each pass also has an overall deadline
   (PASS_DEADLINE_S); clearing reuses the mark list rather than re-reading the tree.
8. Task 33 precondition (N3-M1, N3-M2), before the first CLIENT_CONFIDENTIAL write:
   - the boxes are read BEFORE and AGAIN AFTER the shot, and the union must be black -- a field
     that moved while the overlay was inserted, or a marked form that only got a layout box
     during the shot (a top-layer dialog or popover), shows colour in its post-shot box;
   - besides each marked node's own quads, the quads of every non-blank TEXT node inside it
     (light DOM and shadow roots) must be black -- ink overflowing a marked element's box (a
     zero-height or overflowing contenteditable) is otherwise neither masked nor verified.
     Clipped text whose quads extend past a clipping box can cause a false refusal, never a
     pass with colour in it. At most MAX_TEXT_QUADS text nodes; past it the capture refuses;
   - the PNG is decoded off the event loop (asyncio.to_thread) under VERIFY_DEADLINE_S, a page
     past png.MAX_HEIGHT_PX is refused, and only the rows down to the lowest box are inflated.
9. document.designMode (CLIENT_CONFIDENTIAL only): read per frame in an isolated world (page
   script cannot patch it there). A frame in design mode is masked whole by its owner element;
   the top document in design mode -- or a frame whose mode cannot be read -- refuses or masks
   whole, failing closed.

MAX_MASKED_NODES is defined in heallog.capture (heallog may not import browser/), so the PII
masker (Task 33) can stay under the same bound by masking containers rather than every cell.

PUBLIC and SYNTHETIC captures keep the heuristic floor (step 1 and the step-3 count), per the
R4 ruling, plus step 6 whenever they mask anything. Chromium only: without CDP a capture that
must be verified refuses.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import secrets
from collections import Counter
from collections.abc import Awaitable, Coroutine
from typing import Any
from urllib.parse import urlsplit

from bok_core.boundary.sensitivity import Sensitivity
from playwright.async_api import CDPSession, Locator, Page
from playwright.async_api import Error as PlaywrightError

from selector_drift.browser import png
from selector_drift.browser.driver import PageHandle, _playwright_page
from selector_drift.browser.page_source import MAX_SNAPSHOT_NODES
from selector_drift.heallog.capture import (
    MAX_MASKED_NODES,
    NODE_ATTR,
    NODE_ID_RE,
    BBox,
    CaptureError,
    raised,
)

CALL_TIMEOUT_S = 30
# A bound on how many nodes one screenshot may mask by id is heallog.capture.MAX_MASKED_NODES.
__all__ = ["MAX_MASKED_NODES", "LiveCapture"]
# Light-DOM elements the live pass accepts before transferring the pierced tree.
LIVE_MAX_NODES = MAX_SNAPSHOT_NODES
# Elements one capture may mark (form controls + whole-masked containers + ids).
MAX_LIVE_MARKS = 10_000
MASK_ATTR = "data-sd-mask"
# Overall bound on a mark or clear pass (one CDP call per node, up to MAX_LIVE_MARKS).
PASS_DEADLINE_S = 60
# Pixels trimmed from each side of a box before it must be black (anti-aliased edges).
VERIFY_INSET_PX = 2
# Text nodes inside marked elements whose quads are verified (N3-M2); past it, refuse.
MAX_TEXT_QUADS = 20_000
# The PNG decode and pixel check run off the event loop under this deadline.
VERIFY_DEADLINE_S = 60
_DESIGN_WORLD = "sd-design-mode"
_UNRENDERED_TEXT_PARENTS = frozenset({"script", "style", "noscript", "template", "title"})

Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in full-page CSS px

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


async def _deadline[T](call: Coroutine[Any, Any, T], what: str) -> T:
    try:
        return await asyncio.wait_for(call, PASS_DEADLINE_S)
    except TimeoutError:
        raise CaptureError(f"{what} exceeded {PASS_DEADLINE_S}s; refusing") from None


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


def _text_nodes(marks: list[dict[str, Any]]) -> list[int]:
    """Backend ids of every non-blank text node inside the marked elements (their light DOM
    and every non-user-agent shadow root; never a frame's document, which its owner clips),
    skipping text that is never rendered (script, style, ...)."""
    found: list[int] = []
    seen: set[int] = set()
    stack: list[tuple[dict[str, Any], str]] = []

    def push(node: dict[str, Any], tag: str) -> None:
        stack.extend((child, tag) for child in node.get("children") or [])
        stack.extend(
            (root, tag)
            for root in node.get("shadowRoots") or []
            if root.get("shadowRootType") in ("open", "closed")
        )

    for mark in marks:
        push(mark, str(mark.get("localName") or mark.get("nodeName", "")).lower())
    try:
        while stack:
            node, parent = stack.pop()
            if node.get("nodeType") == 3:
                text = str(node.get("nodeValue") or "")
                backend = int(node["backendNodeId"])
                if text.strip() and parent not in _UNRENDERED_TEXT_PARENTS and backend not in seen:
                    seen.add(backend)
                    found.append(backend)
                continue
            push(node, str(node.get("localName") or "").lower() or parent)
    except (KeyError, TypeError, ValueError):
        raise CaptureError("CDP returned a malformed text node; refusing") from None
    return found


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

    async def optional(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Like send(), but a protocol error is None -- for DOM.getContentQuads, which errors
        on a node with no layout box. A timeout is still a refusal."""
        try:
            reply = await _bounded(self._session.send(method, params), method)
        except PlaywrightError:
            return None
        return reply if isinstance(reply, dict) else None

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

    async def _design_mode_owners(self, cdp: _Cdp) -> list[int]:
        """Backend ids of the owner elements of every frame in design mode (fix 9).

        Read per frame in an isolated world, where page script cannot patch `document`. A
        child frame whose mode cannot be read is masked whole too; the top document in design
        mode -- or unreadable -- refuses (there is no owner to mask)."""
        tree = await cdp.send("Page.getFrameTree", {})
        frames: list[tuple[str, bool]] = []
        try:
            stack: list[tuple[dict[str, Any], bool]] = [(tree["frameTree"], True)]
            while stack:
                node, top = stack.pop()
                frames.append((str(node["frame"]["id"]), top))
                stack.extend((child, False) for child in node.get("childFrames") or [])
        except (KeyError, TypeError):
            raise CaptureError("CDP returned a malformed frame tree; refusing") from None
        owners: list[int] = []
        for frame_id, top in frames:
            mode: object = None
            world = await cdp.optional(
                "Page.createIsolatedWorld", {"frameId": frame_id, "worldName": _DESIGN_WORLD}
            )
            if world is not None and "executionContextId" in world:
                reply = await cdp.optional(
                    "Runtime.evaluate",
                    {
                        "expression": "document.designMode",
                        "contextId": world["executionContextId"],
                        "returnByValue": True,
                    },
                )
                result = (reply or {}).get("result")
                mode = result.get("value") if isinstance(result, dict) else None
            if mode == "off":
                continue
            if top:
                raise CaptureError(
                    "the page is in design mode (or its mode cannot be read): its whole text is "
                    "editable and cannot be masked; refusing"
                )
            owner = await cdp.send("DOM.getFrameOwner", {"frameId": frame_id})
            try:
                owners.append(int(owner["backendNodeId"]))
            except (KeyError, TypeError, ValueError):
                raise CaptureError("CDP returned a malformed frame owner; refusing") from None
        return owners

    async def _mark_cc(
        self, cdp: _Cdp, ids: frozenset[str], nonce: str
    ) -> tuple[list[int], list[int]]:
        """Classify and mark; returns the marked nodes' backend ids (stable across the later
        DOM.getDocument calls, which invalidate frontend node ids), and the backend ids of the
        text nodes inside them (N3-M2)."""
        design = await self._design_mode_owners(cdp)
        marks, id_hits, _ = _classify(await cdp.tree(), ids)
        self._check_ids(ids, id_hits)
        try:
            pairs = [(int(n["nodeId"]), int(n["backendNodeId"])) for n in marks]
        except (KeyError, TypeError, ValueError):
            raise CaptureError("CDP returned a malformed node; refusing") from None
        texts = _text_nodes(marks)
        seen = {backend for _node, backend in pairs}
        extra = [b for b in design if b not in seen]
        if extra:
            pushed = await cdp.send(
                "DOM.pushNodesByBackendIdsToFrontend", {"backendNodeIds": extra}
            )
            node_ids = pushed.get("nodeIds") or []
            if len(node_ids) != len(extra) or not all(node_ids):
                raise CaptureError("a design-mode frame owner could not be resolved; refusing")
            pairs += [(int(n), b) for n, b in zip(node_ids, extra, strict=True)]
        if len(pairs) > MAX_LIVE_MARKS:
            raise CaptureError(f"{len(pairs)} elements to mask exceeds {MAX_LIVE_MARKS}; refusing")

        async def mark_all() -> None:
            for node_id, _backend in pairs:
                await cdp.send(
                    "DOM.setAttributeValue",
                    {"nodeId": node_id, "name": MASK_ATTR, "value": nonce},
                )

        await _deadline(mark_all(), "the mark pass")
        return [backend for _node, backend in pairs], texts

    async def _unmarked_cc(self, cdp: _Cdp, ids: frozenset[str], nonce: str) -> int:
        marks, id_hits, _ = _classify(await cdp.tree(), ids)
        self._check_ids(ids, id_hits)
        return sum(1 for node in marks if _attrs(node).get(MASK_ATTR) != nonce)

    async def _clear_cc(self, cdp: _Cdp, backend: list[int]) -> None:
        """Reuses the mark list: no third full-tree transfer."""
        if not backend:
            return
        pushed = await cdp.send("DOM.pushNodesByBackendIdsToFrontend", {"backendNodeIds": backend})

        async def clear_all() -> None:
            for node_id in pushed.get("nodeIds") or []:
                if node_id:
                    await cdp.send(
                        "DOM.removeAttribute", {"nodeId": int(node_id), "name": MASK_ATTR}
                    )

        await _deadline(clear_all(), "the clear pass")

    async def _marked_by_selector(self, cdp: _Cdp, nonce: str) -> tuple[list[int], list[int]]:
        """PUBLIC/SYNTHETIC: the id pass marked in page script; find those nodes over CDP, as
        backend ids, with the text nodes inside them (N3-M2)."""
        try:
            shallow = await cdp.send("DOM.getDocument", {"depth": 0})
            root_id = int(shallow["root"]["nodeId"])
            found = await cdp.send(
                "DOM.querySelectorAll",
                {"nodeId": root_id, "selector": f'[{MASK_ATTR}="{nonce}"]'},
            )
            nodes: list[dict[str, Any]] = []
            for node_id in found["nodeIds"]:
                described = await cdp.send(
                    "DOM.describeNode", {"nodeId": int(node_id), "depth": -1, "pierce": True}
                )
                nodes.append(described["node"])
            backend = [int(n["backendNodeId"]) for n in nodes]
        except (KeyError, TypeError, ValueError):
            raise CaptureError("CDP returned a malformed document; refusing") from None
        return backend, _text_nodes(nodes)

    # -- pixel verification (fix round 3; Task 33 N3-M1/N3-M2) -------------------------------
    @staticmethod
    async def _boxes(cdp: _Cdp, backend: list[int]) -> tuple[list[Box], float]:
        """Every target node's quads as full-page boxes, and the page's CSS width."""
        try:
            metrics = await cdp.send("Page.getLayoutMetrics", {})
            viewport = metrics["cssLayoutViewport"]
            ox, oy = float(viewport["pageX"]), float(viewport["pageY"])
            width = max(float(metrics["cssContentSize"]["width"]), float(viewport["clientWidth"]))
        except (KeyError, TypeError, ValueError):
            raise CaptureError("CDP returned malformed layout metrics; refusing") from None
        boxes: list[Box] = []

        async def read_all() -> None:
            for target in backend:
                reply = await cdp.optional("DOM.getContentQuads", {"backendNodeId": target})
                if reply is None:
                    continue  # no layout box: it painted nothing
                try:
                    for quad in reply.get("quads") or []:
                        xs = [float(v) for v in quad[0::2]]
                        ys = [float(v) for v in quad[1::2]]
                        boxes.append((min(xs) + ox, min(ys) + oy, max(xs) + ox, max(ys) + oy))
                except (TypeError, ValueError):
                    raise CaptureError("CDP returned a malformed quad; refusing") from None

        await _deadline(read_all(), "reading the mask boxes")
        return boxes, width

    @staticmethod
    def _verify_pixels(shot: bytes, boxes: list[Box], css_width: float) -> None:
        if not boxes:
            return
        try:
            header = png.header(shot)
            scale = header.width / css_width if css_width > 0 else 1.0
            bottom = max(math.ceil(y1 * scale) for _, _, _, y1 in boxes)
            image = png.decode(shot, rows_needed=min(header.height, bottom + 1))
        except png.TooLarge:
            raise CaptureError(
                f"the page is taller than {png.MAX_HEIGHT_PX} px (or wider than "
                f"{png.MAX_WIDTH_PX} px), too large to verify its mask; refusing"
            ) from None
        except ValueError:
            raise CaptureError("the masked screenshot could not be decoded; refusing") from None
        uncovered = 0
        for x0, y0, x1, y1 in boxes:
            left = max(0, math.ceil(x0 * scale) + VERIFY_INSET_PX)
            top = max(0, math.ceil(y0 * scale) + VERIFY_INSET_PX)
            right = min(image.width, math.floor(x1 * scale) - VERIFY_INSET_PX)
            lower = min(len(image.rows), math.floor(y1 * scale) - VERIFY_INSET_PX)
            if right <= left or lower <= top:
                continue  # zero-size, inset away, or off the page: nothing to verify
            if not image.is_black(left, top, right, lower):
                uncovered += 1
        if uncovered:
            raise CaptureError(
                f"the mask did not cover {uncovered} marked box(es) in the screenshot "
                "(overlay removed or hidden, or the content moved or overflowed?); refusing"
            )

    async def _verify(self, shot: bytes, boxes: list[Box], css_width: float) -> None:
        """Off the event loop, under a deadline (Task 33 precondition)."""
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._verify_pixels, shot, boxes, css_width),
                VERIFY_DEADLINE_S,
            )
        except TimeoutError:
            raise CaptureError(
                f"verifying the mask took over {VERIFY_DEADLINE_S}s; refusing"
            ) from None

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
        # Pixel verification needs CDP whenever anything is masked (and always at CC).
        cdp = await self._cdp() if deny_all or ids else None
        backend: list[int] = []
        try:
            targets: list[int] = []  # marked nodes, by backend id
            texts: list[int] = []  # text nodes inside them (N3-M2)
            if deny_all and cdp is not None:
                backend, texts = await self._mark_cc(cdp, mask_node_ids, nonce)
                marked = len(backend)
                targets = backend
            else:
                await self._id_pass("mark", ids, nonce)
                marked = len(ids)
                if cdp is not None:
                    targets, texts = await self._marked_by_selector(cdp, nonce)
            if len(texts) > MAX_TEXT_QUADS:
                raise CaptureError(
                    f"{len(texts)} text nodes inside masked elements exceeds {MAX_TEXT_QUADS}; "
                    "refusing"
                )
            visible = await self._visible_marks(nonce)
            if visible != marked:
                raise CaptureError(
                    f"{marked} elements were marked but the mask can see {visible}; refusing"
                )
            verify = targets + texts
            before, css_width = await self._boxes(cdp, verify) if cdp is not None else ([], 0.0)
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
            # N3-M1: the boxes again, AFTER the shot -- a field that moved, or only got a box,
            # while the shot was taken is checked where it is now as well as where it was.
            after, _ = await self._boxes(cdp, verify) if cdp is not None else ([], 0.0)
            if deny_all and cdp is not None:
                late = await self._unmarked_cc(cdp, mask_node_ids, nonce)
            else:
                late = await self._id_pass("verify", ids, nonce)
            if late:
                raise CaptureError(
                    f"{late} element(s) needing a mask appeared during the screenshot; refusing"
                )
            await self._verify(shot, before + after, css_width)
            return shot
        finally:
            # Removing our marks is housekeeping; the outcome is already decided.
            with contextlib.suppress(CaptureError, PlaywrightError, TimeoutError, KeyError):
                if deny_all and cdp is not None:
                    await self._clear_cc(cdp, backend)
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
