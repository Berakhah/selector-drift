"""LiveCapture: the Capturable a live page offers (Task 19). Browser-backed tests are e2e and run
their async body in a fresh joined thread (pattern: tests/testbench/test_runner_identity.py),
because pytest-playwright's sync fixtures pin a running loop on the main thread."""

from __future__ import annotations

import asyncio
import struct
import threading
import zlib
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, cast

import pytest
from bok_core.boundary.sensitivity import Sensitivity
from playwright.async_api import Page, async_playwright

from selector_drift import browser
from selector_drift.browser.driver import _playwright_page
from selector_drift.browser.live_capture import LiveCapture
from selector_drift.browser.page_source import LivePageSource
from selector_drift.heallog.capture import BBox, CaptureError, capture
from selector_drift.scope.record import AuthorisedTarget

SECRET = "S3CRET-canary-live-91c2"


def _in_thread[T](make: Callable[[], Awaitable[T]], timeout: float = 120) -> T:
    result: list[T] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(cast(Any, make())))
        except BaseException as exc:  # re-raised on the test's own thread below
            error.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(f"the live-browser body did not finish within {timeout} s")
    if error:
        raise error[0]
    return result[0]


def _png_pixels(png: bytes) -> tuple[int, int, int, list[bytes]]:
    """Minimal stdlib PNG decoder (8-bit RGB/RGBA, non-interlaced): width, height, bytes per
    pixel, and the unfiltered scanlines. No Pillow in the dependency set."""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    pos, idat, header = 8, b"", b""
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        tag, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
        if tag == b"IHDR":
            header = data
        elif tag == b"IDAT":
            idat += data
        pos += 12 + length
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", header)
    assert depth == 8 and colour in (2, 6) and interlace == 0
    bpp = 3 if colour == 2 else 4
    raw = zlib.decompress(idat)
    stride = width * bpp
    rows: list[bytes] = []
    prev = bytearray(stride)
    for y in range(height):
        kind = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if kind == 1:
                line[i] = (line[i] + a) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + b) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        rows.append(bytes(line))
        prev = line
    return width, height, bpp, rows


def _region_colours(png: bytes, box: BBox) -> set[tuple[int, int, int]]:
    """The distinct RGB colours strictly inside a box (a 3 px inset avoids anti-aliasing)."""
    width, height, bpp, rows = _png_pixels(png)
    colours: set[tuple[int, int, int]] = set()
    for y in range(int(box.y) + 3, min(height, int(box.y + box.height) - 3)):
        for x in range(int(box.x) + 3, min(width, int(box.x + box.width) - 3)):
            px = rows[y][x * bpp : x * bpp + 3]
            colours.add((px[0], px[1], px[2]))
    return colours


@pytest.mark.e2e
def test_live_capture_records_a_real_box_at_the_fixed_viewport() -> None:
    async def go() -> tuple[tuple[int, int], BBox | None, BBox | None, bytes]:
        async with async_playwright() as p:
            b = await p.chromium.launch()
            page = await b.new_page(viewport={"width": 1280, "height": 900})
            await page.set_content(
                '<table><tr><td data-sd-node="n1" style="width:120px">AB12345678</td></tr></table>'
            )
            cap = LiveCapture(page, await page.content())
            box = await cap.bbox("n1")
            missing = await cap.bbox("n2")
            shot = await cap.screenshot(mask_node_ids=frozenset({"n1"}))
            await b.close()
            return cap.viewport, box, missing, shot

    viewport, box, missing, shot = _in_thread(go)
    assert viewport == (1280, 900)
    assert box is not None and box.width >= 120 and box.height > 0
    assert missing is None
    assert shot.startswith(b"\x89PNG")


@pytest.mark.e2e
def test_a_live_capture_masks_a_password_in_the_screenshot_and_drops_it_from_the_dom() -> None:
    """Never-rule 4 against a real browser. The masker names nothing; the credential floor
    alone must black out the password field's pixels and keep its value out of Evidence.dom."""
    html = (
        '<html><body style="margin:0;background:#ffffff">'
        '<p style="font-size:24px">Claim AB12345678</p>'
        '<input type="password" name="password" '
        f'value="{SECRET}" style="width:320px;height:60px;font-size:28px;'
        'background:#ffffff;color:#000000;border:0">'
        "</body></html>"
    )

    async def go() -> tuple[Any, BBox | None, bytes, bytes]:
        async with async_playwright() as p:
            b = await p.chromium.launch()
            page: Page = await b.new_page(viewport={"width": 1280, "height": 900})
            await page.set_content(html)
            source = await LivePageSource.from_playwright(page)  # stamps data-sd-node
            # The live DOM carries the value as a property, not only an attribute.
            await page.locator("input[type=password]").fill(SECRET)
            password = page.locator("input[type=password]")
            node_id = await password.get_attribute("data-sd-node")
            assert node_id is not None
            cap = LiveCapture(page, source.dom())
            ev = await capture(
                cap,
                run_key="testbench:1:2026-09-07",
                sensitivity=Sensitivity.CLIENT_CONFIDENTIAL,
                masker=lambda dom: frozenset(),
            )
            box = await cap.bbox(node_id)
            unmasked = await page.screenshot(full_page=True, type="png")
            await b.close()
            return ev, box, ev.screenshot.value, unmasked

    ev, box, masked, unmasked = _in_thread(go)
    assert box is not None and box.width >= 300
    assert SECRET not in ev.dom.value
    assert "AB12345678" in ev.dom.value
    assert ev.screenshot.sensitivity is Sensitivity.CLIENT_CONFIDENTIAL
    assert len(ev.masked_node_ids) == 1
    assert _region_colours(masked, box) == {(0, 0, 0)}
    assert _region_colours(unmasked, box) != {(0, 0, 0)}  # the mask, not the page, is black


@pytest.mark.unit
def test_live_capture_refuses_an_unminted_node_id_before_touching_the_page() -> None:
    class Untouchable:
        viewport_size: ClassVar[dict[str, int]] = {"width": 1280, "height": 900}

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"the page was touched: {name}")

    cap = LiveCapture(cast(Page, Untouchable()), "<html></html>")
    with pytest.raises(CaptureError):
        asyncio.run(cap.bbox('"] , script'))
    with pytest.raises(CaptureError):
        asyncio.run(cap.screenshot(mask_node_ids=frozenset({"n1", "x'] *"})))


@pytest.mark.e2e
def test_the_driver_opens_every_context_at_the_fixed_viewport(
    authorised_testbench: AuthorisedTarget,
) -> None:
    async def go() -> object:
        async with browser.driver.open(authorised_testbench) as session:
            raw = _playwright_page(session.page)
            return raw.viewport_size, await raw.evaluate("window.devicePixelRatio")

    assert _in_thread(go) == ({"width": 1280, "height": 900}, 1)
