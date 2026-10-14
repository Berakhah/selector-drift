"""LiveCapture: the Capturable a live page offers (Task 19). Browser-backed tests are e2e and run
their async body in a fresh joined thread (pattern: tests/testbench/test_runner_identity.py),
because pytest-playwright's sync fixtures pin a running loop on the main thread."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from bok_core.boundary.sensitivity import Sensitivity
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from selector_drift import browser
from selector_drift.browser.driver import _playwright_page
from selector_drift.browser.live_capture import LiveCapture
from selector_drift.browser.page_source import LivePageSource
from selector_drift.browser.png import decode as png_decode
from selector_drift.heallog.capture import BBox, CaptureError, capture
from selector_drift.scope.gate import authorise
from selector_drift.scope.record import AuthorisedTarget, Basis, RateLimit, ScopeRecord
from tests.browser.conftest import TARGET

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
    """The product's own stdlib decoder (selector_drift/browser/png.py), as the tests see it."""
    image = png_decode(png)
    return image.width, image.height, image.channels, image.rows


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
            cap = LiveCapture(page, await page.content(), sensitivity=Sensitivity.SYNTHETIC)
            box = await cap.bbox("n1")
            missing = await cap.bbox("n2")
            shot = await cap.masked_screenshot(mask_node_ids=frozenset({"n1"}))
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
            cap = LiveCapture(page, source.dom(), sensitivity=Sensitivity.SYNTHETIC)
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
    assert ev.dom.sensitivity is Sensitivity.CLIENT_CONFIDENTIAL  # raised by the caller
    assert len(ev.masked_node_ids) == 1
    assert _region_colours(masked, box) == {(0, 0, 0)}
    assert _region_colours(unmasked, box) != {(0, 0, 0)}  # the mask, not the page, is black


@pytest.mark.unit
def test_live_capture_refuses_an_unminted_node_id_before_touching_the_page() -> None:
    class Untouchable:
        viewport_size: ClassVar[dict[str, int]] = {"width": 1280, "height": 900}

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"the page was touched: {name}")

    cap = LiveCapture(cast(Page, Untouchable()), "<html></html>", sensitivity=Sensitivity.SYNTHETIC)
    with pytest.raises(CaptureError):
        asyncio.run(cap.bbox('"] , script'))
    with pytest.raises(CaptureError):
        asyncio.run(cap.masked_screenshot(mask_node_ids=frozenset({"n1", "x'] *"})))


@pytest.mark.e2e
def test_the_driver_opens_every_context_at_the_fixed_viewport(
    authorised_testbench: AuthorisedTarget,
) -> None:
    async def go() -> object:
        async with browser.driver.open(authorised_testbench) as session:
            raw = _playwright_page(session.page)
            return raw.viewport_size, await raw.evaluate("window.devicePixelRatio")

    assert _in_thread(go) == ({"width": 1280, "height": 900}, 1)


# -- fix round 1, R4: the screenshot floor fails closed (S-I1) ---------------------------------

MAGENTA = (255, 0, 255)
PAINT = "background:#ff00ff;color:#ff00ff;border:0;width:300px;height:60px;display:block"
CC = Sensitivity.CLIENT_CONFIDENTIAL


def _magenta(png: bytes) -> int:
    _, _, bpp, rows = _png_pixels(png)
    return sum(
        1
        for row in rows
        for i in range(0, len(row), bpp)
        if (row[i], row[i + 1], row[i + 2]) == MAGENTA
    )


async def _scenario(
    html: str,
    after_snapshot: str | None,
    sensitivity: Sensitivity = CC,
    requested: Sensitivity | None = None,
) -> tuple[bytes, bytes]:
    """Snapshot (stamping data-sd-node), run `after_snapshot` on the live page, then capture.
    Returns (stored screenshot, an unmasked screenshot of the same page for the baseline)."""
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            page = await b.new_page(viewport={"width": 1280, "height": 900})
            await page.set_content(html)
            await page.wait_for_load_state("load")
            source = await LivePageSource.from_playwright(page)
            if after_snapshot:
                await page.evaluate(after_snapshot)
            cap = LiveCapture(page, source.dom(), sensitivity=sensitivity)
            ev = await capture(
                cap,
                run_key="testbench:1:2026-09-07",
                masker=lambda d: frozenset(),
                sensitivity=requested,
            )
            unmasked = await page.screenshot(full_page=True, type="png")
            return ev.screenshot.value, unmasked
        finally:
            await b.close()


def _field(kind: str = 'name="code"') -> str:
    return f'<input {kind} value="123456" style="{PAINT}">'


def _shadow(mode: str) -> str:
    inner = _field().replace('"', '\\"')
    return (
        '<html><body style="margin:0"><div id="host"></div><script>'
        f"document.getElementById('host').attachShadow({{mode: '{mode}'}}).innerHTML = "
        f'"{inner}";'
        "</script></body></html>"
    )


_SRCDOC = ('<body style="margin:0">' + _field() + "</body>").replace('"', "&quot;")
_STRIP_STAMPS = (
    "(() => { const w = document.getElementById('w');"
    ' w.innerHTML = w.innerHTML.replace(/ data-sd-node="[^"]*"/g, \'\'); })()'
)
_MODAL = (
    "(() => { const d = document.createElement('div'); d.setAttribute('role', 'dialog');"
    " d.innerHTML = " + repr(_field('type="password"')) + "; document.body.appendChild(d); })()"
)

SCENARIOS: dict[str, tuple[str, str | None]] = {
    "open-shadow-root": (_shadow("open"), None),
    "closed-shadow-root": (_shadow("closed"), None),
    "same-origin-iframe": (
        '<html><body style="margin:0"><iframe style="width:400px;height:120px;border:0" '
        f'srcdoc="{_SRCDOC}"></iframe></body></html>',
        None,
    ),
    "re-rendered-after-snapshot": (
        f'<html><body style="margin:0"><div id="w">{_field()}</div></body></html>',
        _STRIP_STAMPS,
    ),
    "modal-after-snapshot": ('<html><body style="margin:0"><p>claims</p></body></html>', _MODAL),
    # fix round 2, N-M1: page script that patches the DOM built-ins after the snapshot
    "shadowroot-getter-patched": (
        _shadow("open"),
        "Object.defineProperty(Element.prototype, 'shadowRoot',"
        " {get() { return null; }, configurable: true})",
    ),
    "queryselectorall-patched": (
        f'<html><body style="margin:0"><div>{_field()}</div></body></html>',
        "(() => { for (const proto of [Document.prototype, Element.prototype,"
        " DocumentFragment.prototype]) { const real = proto.querySelectorAll;"
        " proto.querySelectorAll = function (s) {"
        " return s === '*' ? document.createDocumentFragment().childNodes"
        " : real.call(this, s); }; } })()",
    ),
}


@pytest.mark.e2e
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_a_client_confidential_screenshot_masks_what_the_snapshot_never_saw(name: str) -> None:
    html, after = SCENARIOS[name]
    stored, unmasked = _in_thread(lambda: _scenario(html, after))
    print(f"{name}: magenta unmasked={_magenta(unmasked)} stored={_magenta(stored)}")
    assert _magenta(unmasked) > 5_000, f"{name}: the field never rendered -- vacuous"
    assert _magenta(stored) == 0, f"{name}: {_magenta(stored)} magenta pixels survived"


@pytest.mark.e2e
def test_a_cross_origin_iframe_is_masked_wholesale(bench: Any) -> None:
    html = (
        '<html><body style="margin:0"><iframe '
        f'src="{bench.base_url}" style="width:400px;height:160px;'
        'border:12px solid #ff00ff;background:#ff00ff"></iframe></body></html>'
    )
    stored, unmasked = _in_thread(lambda: _scenario(html, None))
    print(f"cross-origin-iframe: magenta unmasked={_magenta(unmasked)} stored={_magenta(stored)}")
    assert _magenta(unmasked) > 5_000
    assert _magenta(stored) == 0


@pytest.mark.e2e
def test_a_stale_mask_id_refuses_the_capture() -> None:
    """R4.1: a credential the snapshot named, re-rendered since, would be masked by nothing."""
    html = (
        f'<html><body style="margin:0"><div id="w">{_field('type="password"')}</div></body></html>'
    )
    with pytest.raises(CaptureError, match="exactly one"):
        _in_thread(lambda: _scenario(html, _STRIP_STAMPS, Sensitivity.SYNTHETIC))


@pytest.mark.e2e
def test_a_re_rendered_control_at_client_confidential_is_masked_not_refused() -> None:
    """N-M4 (Task 33): at CLIENT_CONFIDENTIAL the default-deny pass masks every control by
    itself, so a control whose id stamp was lost is masked, not a reason to refuse."""
    html = (
        f'<html><body style="margin:0"><div id="w">{_field('type="password"')}</div></body></html>'
    )
    stored, unmasked = _in_thread(lambda: _scenario(html, _STRIP_STAMPS, CC))
    assert _magenta(unmasked) > 5_000
    assert _magenta(stored) == 0


@pytest.mark.e2e
def test_the_live_pass_refuses_past_its_node_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    from selector_drift.browser import live_capture

    monkeypatch.setattr(live_capture, "LIVE_MAX_NODES", 3)
    html = "<html><body>" + "<p>x</p>" * 10 + "</body></html>"
    with pytest.raises(CaptureError, match="ceiling"):
        _in_thread(lambda: _scenario(html, None))


# -- fix round 1, R3: the label comes from the scope record (S-I5) ------------------------------


@pytest.fixture
def authorised_client(
    bench: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AuthorisedTarget:
    """The driver fixture's target, authorised as a CLIENT portal -- so CLIENT_CONFIDENTIAL."""
    doc = tmp_path / "authorisation.pdf"
    doc.write_bytes(b"countersigned")
    today = datetime.now(UTC).date()
    record = ScopeRecord(
        target=TARGET,
        basis=Basis.CLIENT_AUTHORISED,
        authorising_party="selector-drift test suite",
        authorised_on=today - timedelta(days=1),
        expires_on=today + timedelta(days=365),
        evidence_uri=doc.as_uri(),
        evidence_sha256=hashlib.sha256(b"countersigned").hexdigest(),
        origins=[bench.origin],
        rate_limit=RateLimit(requests=1, per_seconds=30),
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required",
        legal_review="self_drafted",
        evidence_retention_days=90,
    )
    monkeypatch.setattr("selector_drift.browser.registry.url_for", lambda _t: bench.base_url)
    return authorise(record, today=today)


@pytest.mark.e2e
def test_a_client_target_captured_as_public_stores_client_confidential(
    authorised_client: AuthorisedTarget,
) -> None:
    assert authorised_client.sensitivity is CC

    async def go() -> Any:
        async with browser.driver.open(authorised_client) as session:
            source = await LivePageSource.snapshot(session.page)
            cap = LiveCapture.of(session.page, source.dom())
            return await capture(
                cap,
                run_key="driver-fixture:1:2026-09-07",
                sensitivity=Sensitivity.PUBLIC,
                masker=lambda d: frozenset(),
            )

    ev = _in_thread(go)
    assert ev.dom.sensitivity is CC and ev.screenshot.sensitivity is CC


# -- fix round 2, N-M2: CDP failures refuse as CaptureError --------------------------------


class _FakeCDP:
    def __init__(self, reply: object) -> None:
        self.reply = reply

    async def send(self, method: str, params: object = None) -> object:
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply

    async def detach(self) -> None:
        return None


class _FakeContext:
    def __init__(self, cdp: _FakeCDP) -> None:
        self.cdp = cdp

    async def new_cdp_session(self, page: object) -> _FakeCDP:
        return self.cdp


class _FakePage:
    viewport_size: ClassVar[dict[str, int]] = {"width": 1280, "height": 900}

    def __init__(self, cdp: _FakeCDP) -> None:
        self.context = _FakeContext(cdp)
        self.frames: list[object] = []


@pytest.mark.unit
@pytest.mark.parametrize(
    "reply",
    [PlaywrightError("send failed"), {}, {"root": "not-a-node"}, {"nodeIds": None}],
    ids=["send-raises", "empty-reply", "malformed-root", "malformed-ids"],
)
def test_a_cdp_failure_refuses_as_a_capture_error(reply: object) -> None:
    page = _FakePage(_FakeCDP(reply))
    cap = LiveCapture(cast(Page, page), "<html></html>", sensitivity=CC)
    with pytest.raises(CaptureError):
        asyncio.run(cap.masked_screenshot(mask_node_ids=frozenset()))


@pytest.mark.e2e
def test_a_label_raised_to_client_confidential_also_raises_the_pixel_mask() -> None:
    """Fix round 2: a caller who raises a SYNTHETIC page to CLIENT_CONFIDENTIAL gets the
    default-deny mask too, not a CLIENT_CONFIDENTIAL label over heuristic-floor pixels."""
    html, after = SCENARIOS["open-shadow-root"]
    stored, unmasked = _in_thread(
        lambda: _scenario(html, after, Sensitivity.SYNTHETIC, requested=CC)
    )
    assert _magenta(unmasked) > 5_000
    assert _magenta(stored) == 0


# -- fix round 3 (a): the mask's pixels are verified after the shot --------------------------

_OVERLAY_ATTACKS = {
    "mutation-observer-removes-overlay": (
        "new MutationObserver((ms) => { for (const m of ms) for (const n of m.addedNodes)"
        " if (n.nodeType === 1 && n.localName.startsWith('x-')) n.remove(); })"
        ".observe(document, {childList: true, subtree: true})"
    ),
    "css-hides-overlay": (
        "(() => { const s = document.createElement('style');"
        " s.textContent = 'x-pw-glass{display:none!important}';"
        " document.head.appendChild(s); })()"
    ),
}


@pytest.mark.e2e
@pytest.mark.parametrize("attack", list(_OVERLAY_ATTACKS))
def test_a_removed_or_hidden_mask_overlay_refuses_the_capture(attack: str) -> None:
    html = (
        '<html><head></head><body style="margin:0"><p>claims</p>'
        f"<div>{_field()}</div></body></html>"
    )
    with pytest.raises(CaptureError, match="did not cover"):
        _in_thread(lambda: _scenario(html, _OVERLAY_ATTACKS[attack]))


_GEOMETRY = {
    "css-transform": (
        f'<html><body style="margin:0;padding:40px"><div style="transform:rotate(12deg) '
        f'scale(1.2);width:320px">{_field()}</div></body></html>'
    ),
    "position-fixed-on-long-page": (
        '<html><body style="margin:0;height:3000px"><div style="position:fixed;bottom:0;'
        f'left:0">{_field()}</div></body></html>'
    ),
    "position-sticky-on-long-page": (
        '<html><body style="margin:0;height:3000px"><div style="height:200px"></div>'
        f'<div style="position:sticky;top:0">{_field()}</div></body></html>'
    ),
}


@pytest.mark.e2e
@pytest.mark.parametrize("shape", list(_GEOMETRY))
def test_unusual_geometry_passes_clean_or_refuses_never_leaks(shape: str) -> None:
    try:
        stored, unmasked = _in_thread(lambda: _scenario(_GEOMETRY[shape], None))
    except CaptureError as refused:
        print(f"{shape}: refused ({refused})")
        return
    print(f"{shape}: magenta unmasked={_magenta(unmasked)} stored={_magenta(stored)}")
    assert _magenta(unmasked) > 5_000
    assert _magenta(stored) == 0


@pytest.mark.e2e
def test_a_synthetic_capture_with_a_mask_is_pixel_verified_too() -> None:
    password = _field('type="password"')
    html = f'<html><body style="margin:0"><div>{password}</div></body></html>'
    with pytest.raises(CaptureError, match="did not cover"):
        _in_thread(
            lambda: _scenario(html, _OVERLAY_ATTACKS["css-hides-overlay"], Sensitivity.SYNTHETIC)
        )


@pytest.mark.unit
def test_the_png_decoder_reads_what_it_is_given() -> None:
    from selector_drift.browser.png import decode
    from selector_drift.heallog.capture import BLANK_PNG

    image = decode(BLANK_PNG)
    assert (image.width, image.height) == (1, 1)
    with pytest.raises(ValueError):
        decode(b"not a png")


@pytest.mark.e2e
@pytest.mark.parametrize(
    "name", ["open-shadow-root", "closed-shadow-root", "same-origin-iframe", "modal-after-snapshot"]
)
def test_pixel_verification_is_not_vacuous(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every scenario's field yields at least one verified box -- the check really ran."""
    seen: list[int] = []
    real = LiveCapture._verify_pixels

    def spy(shot: bytes, boxes: list[Any], css_width: float) -> None:
        seen.append(len(boxes))
        real(shot, boxes, css_width)

    monkeypatch.setattr(LiveCapture, "_verify_pixels", staticmethod(spy))
    html, after = SCENARIOS[name]
    _in_thread(lambda: _scenario(html, after))
    assert seen and seen[0] >= 1, f"{name}: no box was verified"


# -- Task 33 precondition: N3-M1 (boxes re-read after the shot), N3-M2 (text-node quads),
#    bounded decode, designMode ---------------------------------------------------------------

_ON_GLASS = (
    "new MutationObserver((ms) => {{ for (const m of ms) for (const n of m.addedNodes)"
    " if (n.nodeType === 1 && n.localName.startsWith('x-')) {{ {action} }} }})"
    ".observe(document, {{childList: true, subtree: true}})"
)
_DURING_SHOT = {
    # The field moves when Playwright inserts its overlay: the overlay covers where it WAS.
    "moved-when-the-overlay-is-inserted": (
        '<html><body style="margin:0;height:1200px"><div id="w" style="position:absolute;'
        f'top:0;left:0">{_field()}</div></body></html>',
        _ON_GLASS.format(action="document.getElementById('w').style.top = '500px';"),
    ),
    # A marked form with no layout box before the shot opens in the top layer during it.
    "popover-opened-during-the-shot": (
        '<html><body style="margin:0"><div id="p" popover style="margin:0;padding:0;'
        f'border:0;top:300px;left:0">{_field()}</div></body></html>',
        _ON_GLASS.format(action="document.getElementById('p').showPopover();"),
    ),
}

_OVERFLOW = {
    "zero-height-contenteditable": (
        '<html><body style="margin:0"><div contenteditable="true" style="height:0;'
        'width:400px;font-size:48px;color:#ff00ff">MMMMMM</div></body></html>'
    ),
    "overflowing-contenteditable": (
        '<html><body style="margin:0"><div contenteditable="true" style="height:20px;'
        'width:400px;font-size:40px;line-height:44px;color:#ff00ff">MMMM<br>MMMM<br>MMMM'
        "</div></body></html>"
    ),
}


@pytest.mark.e2e
@pytest.mark.parametrize("name", list(_DURING_SHOT))
def test_a_field_that_moves_or_appears_during_the_shot_refuses(name: str) -> None:
    """N3-M1: verified where the field is after the shot, not only where it was before."""
    html, during = _DURING_SHOT[name]
    try:
        stored, _unmasked = _in_thread(lambda: _scenario(html, during))
    except CaptureError as refused:
        print(f"{name}: refused ({refused})")
        return
    print(f"{name}: magenta stored={_magenta(stored)}")
    assert _magenta(stored) == 0, f"{name}: {_magenta(stored)} magenta pixels leaked silently"


@pytest.mark.e2e
@pytest.mark.parametrize("name", list(_OVERFLOW))
def test_text_overflowing_a_marked_box_refuses(name: str) -> None:
    """N3-M2: the quads of the text inside a marked element are verified too."""
    html = _OVERFLOW[name]
    try:
        stored, _ = _in_thread(lambda: _scenario(html, None))
    except CaptureError as refused:
        assert "did not cover" in str(refused)
        return
    assert _magenta(stored) == 0, f"{name}: {_magenta(stored)} magenta pixels leaked silently"


@pytest.mark.e2e
@pytest.mark.parametrize("name", list(_OVERFLOW))
def test_the_overflow_scenarios_are_not_vacuous(name: str) -> None:
    """Without the text-node quads the same pages leak: the verification above is what
    refuses them."""
    from selector_drift.browser import live_capture

    real = live_capture._text_nodes
    try:
        live_capture._text_nodes = lambda marks: []  # type: ignore[assignment]
        stored, _ = _in_thread(lambda: _scenario(_OVERFLOW[name], None))
    finally:
        live_capture._text_nodes = real
    assert _magenta(stored) > 0, f"{name}: the text never overflowed -- vacuous"


@pytest.mark.e2e
def test_a_frame_in_design_mode_is_masked_whole() -> None:
    body = '<body style="margin:0"><p style="' + PAINT + '">editable notes</p></body>'
    html = (
        '<html><body style="margin:0"><iframe style="width:400px;height:120px;border:0" '
        f'srcdoc="{body.replace('"', "&quot;")}"></iframe></body></html>'
    )
    stored, unmasked = _in_thread(
        lambda: _scenario(html, "document.querySelector('iframe').contentDocument.designMode='on'")
    )
    assert _magenta(unmasked) > 5_000
    assert _magenta(stored) == 0


@pytest.mark.e2e
def test_a_top_document_in_design_mode_refuses() -> None:
    html = f'<html><body style="margin:0"><p style="{PAINT}">notes</p></body></html>'
    with pytest.raises(CaptureError, match="design mode"):
        _in_thread(lambda: _scenario(html, "document.designMode = 'on'"))


@pytest.mark.e2e
def test_a_page_past_the_height_ceiling_refuses() -> None:
    html = (
        '<html><body style="margin:0;height:40000px"><div style="position:absolute;'
        f'top:39000px">{_field()}</div></body></html>'
    )
    with pytest.raises(CaptureError, match="taller than"):
        _in_thread(lambda: _scenario(html, None))


@pytest.mark.e2e
def test_an_editable_role_is_masked_by_default_deny() -> None:
    """Fix round 1: role=textbox (no contenteditable attribute) is typed input too."""
    html = (
        f'<html><body style="margin:0"><div role="textbox" style="{PAINT}">123456</div>'
        "</body></html>"
    )
    stored, unmasked = _in_thread(lambda: _scenario(html, None))
    assert _magenta(unmasked) > 5_000
    assert _magenta(stored) == 0
