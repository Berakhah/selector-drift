import asyncio
import hashlib
import inspect
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from bok_core.boundary.sensitivity import Labelled, Sensitivity

from selector_drift.heallog import replay as replay_mod
from selector_drift.heallog.bundle import NodeBox, read_heal_manifest
from selector_drift.heallog.capture import (
    VIEWPORT,
    BBox,
    CaptureError,
    Evidence,
    capture,
    credential_input_nodes,
    record_boxes,
)
from selector_drift.heallog.replay import ExpectedHeal, replay_bundle
from selector_drift.resolve.page import StaticPageSource
from tests.heallog.conftest import CLAIM_NUMBER, RUN_KEY, CapturedHeal, write_test_bundle

LOGIN_HTML = """<html><body><form>
<input data-sd-node="n1" name="username" value="demo">
<input data-sd-node="n2" type="password" name="password" value="hunter2">
<input data-sd-node="n3" type="hidden" name="csrf" value="tok-123">
<span data-sd-node="n4">Test Person One</span></form></body></html>"""

# A recognisable secret in every shape a credential input takes (CLAUDE.md never-rule 4).
SECRET = "S3CRET-canary-7f3a"
CREDENTIAL_SHAPES_HTML = f"""<html><body><form>
<input data-sd-node="c1" type="password" value="{SECRET}-pw">
<input data-sd-node="c2" autocomplete="one-time-code" value="{SECRET}-otc">
<input data-sd-node="c3" name="otp_code" value="{SECRET}-otp">
<label for="pin-box">PIN</label><input data-sd-node="c4" id="pin-box" value="{SECRET}-pin">
<input data-sd-node="c5" aria-label="API token" value="{SECRET}-tok">
<label>Secret answer <input data-sd-node="c6" value="{SECRET}-ans"></label>
<input data-sd-node="c7" type="hidden" name="state" value="{SECRET}-hid">
<input data-sd-node="c8" autocomplete="section-a current-password" value="{SECRET}-cur">
<textarea data-sd-node="c9" name="recoverySecret">{SECRET}-txt</textarea>
<input data-sd-node="k1" name="shipping_address" value="1 Test Street">
<input data-sd-node="k2" name="search" placeholder="Search claims" value="AB12345678">
</form></body></html>"""


@dataclass
class FakePage:
    html: str
    size: tuple[int, int] = VIEWPORT
    sensitivity: Sensitivity = Sensitivity.SYNTHETIC
    boxes: dict[str, BBox] = field(default_factory=dict)
    masks_seen: list[frozenset[str]] = field(default_factory=list)
    bbox_calls: list[str] = field(default_factory=list)
    labels_seen: list[Sensitivity] = field(default_factory=list)

    @property
    def viewport(self) -> tuple[int, int]:
        return self.size

    def dom(self) -> str:
        return self.html

    async def masked_screenshot(
        self, *, mask_node_ids: frozenset[str], sensitivity: Sensitivity
    ) -> bytes:
        self.labels_seen.append(sensitivity)
        self.masks_seen.append(mask_node_ids)
        return b"\x89PNG-fake"

    async def bbox(self, node_id: str) -> BBox | None:
        self.bbox_calls.append(node_id)
        return self.boxes.get(node_id)


def _names_n4(dom: str) -> frozenset[str]:
    return frozenset({"n4"})


def _names_nothing(dom: str) -> frozenset[str]:
    return frozenset()


@pytest.mark.unit
def test_capture_refuses_a_page_not_at_the_fixed_viewport() -> None:
    page = FakePage(LOGIN_HTML, size=(1920, 1080))
    with pytest.raises(CaptureError, match="viewport"):
        asyncio.run(
            capture(page, run_key=RUN_KEY, sensitivity=Sensitivity.SYNTHETIC, masker=_names_n4)
        )


@pytest.mark.unit
def test_capture_masks_every_credential_input_and_what_the_masker_names() -> None:
    page = FakePage(LOGIN_HTML)
    ev = asyncio.run(
        capture(page, run_key=RUN_KEY, sensitivity=Sensitivity.SYNTHETIC, masker=_names_n4)
    )
    assert page.masks_seen == [frozenset({"n2", "n3", "n4"})]
    assert ev.masked_node_ids == frozenset({"n2", "n3", "n4"})


@pytest.mark.unit
def test_a_credential_value_never_reaches_the_stored_dom_whatever_the_masker_says() -> None:
    """Never-rule 4. The masker names nothing; the credential floor still masks the password
    and the hidden token in the screenshot AND drops their values from Evidence.dom, while
    the page's non-credential content survives for replay."""
    page = FakePage(LOGIN_HTML)
    ev = asyncio.run(
        capture(
            page,
            run_key=RUN_KEY,
            sensitivity=Sensitivity.SYNTHETIC,
            masker=_names_nothing,
        )
    )
    assert page.masks_seen == [frozenset({"n2", "n3"})]
    assert ev.masked_node_ids == frozenset({"n2", "n3"})
    assert "hunter2" not in ev.dom.value
    assert "tok-123" not in ev.dom.value
    assert 'value="demo"' in ev.dom.value and "Test Person One" in ev.dom.value
    assert ev.sha256 == hashlib.sha256(ev.dom.value.encode("utf-8")).hexdigest()
    assert "hunter2" not in repr(ev) and "hunter2" not in str(ev)


@pytest.mark.unit
def test_a_client_confidential_capture_drops_every_form_value() -> None:
    """R5: default-deny. Even the username, which no heuristic calls a credential."""
    page = FakePage(LOGIN_HTML, sensitivity=Sensitivity.CLIENT_CONFIDENTIAL)
    ev = asyncio.run(capture(page, run_key=RUN_KEY, masker=_names_nothing))
    for value in ("hunter2", "tok-123", "demo"):
        assert value not in ev.dom.value
    assert "Test Person One" in ev.dom.value and 'data-sd-node="n1"' in ev.dom.value


@pytest.mark.unit
def test_every_credential_shape_is_found_masked_and_scrubbed() -> None:
    found = credential_input_nodes(CREDENTIAL_SHAPES_HTML)
    assert found == frozenset({f"c{i}" for i in range(1, 10)})
    page = FakePage(CREDENTIAL_SHAPES_HTML)
    ev = asyncio.run(
        capture(page, run_key=RUN_KEY, sensitivity=Sensitivity.SYNTHETIC, masker=_names_nothing)
    )
    assert page.masks_seen == [found]
    assert SECRET not in ev.dom.value
    assert "1 Test Street" in ev.dom.value and "AB12345678" in ev.dom.value


@pytest.mark.unit
def test_a_credential_input_that_cannot_be_masked_refuses_the_capture() -> None:
    """A password field with no node id cannot be named in the screenshot's mask list, so the
    screenshot would show it: refuse rather than emit it."""
    html = '<html><body><input type="password" value="hunter2"></body></html>'
    page = FakePage(html)
    with pytest.raises(CaptureError, match="credential"):
        asyncio.run(
            capture(page, run_key=RUN_KEY, sensitivity=Sensitivity.SYNTHETIC, masker=_names_n4)
        )
    assert page.masks_seen == []


@pytest.mark.unit
def test_from_static_scrubs_credentials_too() -> None:
    ev = Evidence.from_static(LOGIN_HTML, run_key=RUN_KEY, sensitivity=Sensitivity.SYNTHETIC)
    assert "hunter2" not in ev.dom.value and "tok-123" not in ev.dom.value
    assert ev.masked_node_ids == frozenset({"n2", "n3"})


@pytest.mark.unit
def test_capture_labels_both_halves_where_they_are_opened() -> None:
    """Contract 1.1: the label attaches where the file is opened."""
    ev = asyncio.run(
        capture(
            FakePage(LOGIN_HTML),
            run_key=RUN_KEY,
            sensitivity=Sensitivity.CLIENT_CONFIDENTIAL,
            masker=_names_n4,
        )
    )
    assert ev.dom.sensitivity is Sensitivity.CLIENT_CONFIDENTIAL
    assert ev.screenshot.sensitivity is Sensitivity.CLIENT_CONFIDENTIAL
    assert ev.viewport == (1280, 900)


@pytest.mark.unit
def test_evidence_cannot_carry_an_unlabelled_half() -> None:
    ev = Evidence.from_static("<html></html>", run_key=RUN_KEY, sensitivity=Sensitivity.SYNTHETIC)
    with pytest.raises(TypeError, match="Labelled"):
        replace(ev, dom="<html></html>")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Labelled"):
        replace(ev, screenshot=b"\x89PNG")  # type: ignore[arg-type]


@pytest.mark.unit
def test_boxes_are_recorded_from_the_live_page_not_recomputed_on_replay(
    captured_heal: CapturedHeal, tmp_path: Path
) -> None:
    """API spec 7.2 / 7.5."""
    box = BBox(x=10, y=40, width=120, height=24)
    page = FakePage(captured_heal.evidence.dom.value, boxes={captured_heal.node_id: box})
    ev = asyncio.run(
        record_boxes(captured_heal.evidence, page, {"claim_number": captured_heal.node_id})
    )
    assert ev.boxes == {"claim_number": box}
    assert page.bbox_calls == [captured_heal.node_id]

    # Replay has no layout engine and never computes one.
    assert not hasattr(StaticPageSource, "bbox")
    assert "getBoundingClientRect" not in inspect.getsource(replay_mod)
    absurd = BBox(x=-1, y=-1, width=0, height=0)
    bundle = write_test_bundle(
        tmp_path / "heals" / "1",
        captured_heal.evidence.dom.value,
        nodes={"claim_number": NodeBox(node_id=captured_heal.node_id, bbox=absurd)},
    )
    replay_bundle(
        bundle,
        CLAIM_NUMBER,
        captured_heal.cache,
        ExpectedHeal(captured_heal.winning_rung, captured_heal.node_id),
    )
    assert read_heal_manifest(bundle).after.nodes["claim_number"].bbox == absurd


@pytest.mark.unit
def test_a_node_id_that_could_break_out_of_a_selector_is_refused(
    captured_heal: CapturedHeal,
) -> None:
    page = FakePage(captured_heal.evidence.dom.value)
    with pytest.raises(CaptureError):
        asyncio.run(record_boxes(captured_heal.evidence, page, {"x": '"] , script'}))
    assert page.bbox_calls == []


@pytest.mark.unit
def test_from_static_hashes_the_dom_it_carries() -> None:
    ev = Evidence.from_static("<html></html>", run_key=RUN_KEY, sensitivity=Sensitivity.SYNTHETIC)
    assert ev.sha256 == hashlib.sha256(b"<html></html>").hexdigest()


# -- fix round 1 ------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_caller_may_raise_the_label_never_lower_it() -> None:
    """R3 / S-I5. The page's label comes from the scope record; a caller's request is a floor."""
    confidential = FakePage(LOGIN_HTML, sensitivity=Sensitivity.CLIENT_CONFIDENTIAL)
    ev = asyncio.run(
        capture(confidential, run_key=RUN_KEY, sensitivity=Sensitivity.PUBLIC, masker=_names_n4)
    )
    assert ev.dom.sensitivity is Sensitivity.CLIENT_CONFIDENTIAL
    assert ev.screenshot.sensitivity is Sensitivity.CLIENT_CONFIDENTIAL

    synthetic = FakePage(LOGIN_HTML, sensitivity=Sensitivity.SYNTHETIC)
    raised = asyncio.run(
        capture(
            synthetic,
            run_key=RUN_KEY,
            sensitivity=Sensitivity.CLIENT_CONFIDENTIAL,
            masker=_names_n4,
        )
    )
    assert raised.dom.sensitivity is Sensitivity.CLIENT_CONFIDENTIAL
    assert synthetic.labels_seen == [Sensitivity.CLIENT_CONFIDENTIAL]  # the pixels follow
    plain = asyncio.run(capture(FakePage(LOGIN_HTML), run_key=RUN_KEY, masker=_names_n4))
    assert plain.dom.sensitivity is Sensitivity.SYNTHETIC


@pytest.mark.unit
def test_evidence_refuses_halves_with_different_labels() -> None:
    ev = Evidence.from_static("<html></html>", run_key=RUN_KEY, sensitivity=Sensitivity.SYNTHETIC)
    with pytest.raises(ValueError, match="sensitivity"):
        replace(ev, screenshot=Labelled(b"\x89PNG", Sensitivity.CLIENT_CONFIDENTIAL))


@pytest.mark.unit
def test_a_failing_masker_becomes_a_capture_error_without_its_text() -> None:
    """R6 / S-M1. The masker sees the raw DOM; its message must not reach an alert."""

    def leaky(dom: str) -> frozenset[str]:
        raise ValueError(f"masker saw {dom}")

    with pytest.raises(CaptureError) as caught:
        asyncio.run(capture(FakePage(LOGIN_HTML), run_key=RUN_KEY, masker=leaky))
    assert "hunter2" not in str(caught.value) and "masker saw" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.unit
def test_from_static_refuses_client_confidential() -> None:
    """R6 / S-M3. from_static cannot have masked a real screenshot, so it may not claim to."""
    with pytest.raises(CaptureError, match="CLIENT_CONFIDENTIAL"):
        Evidence.from_static(
            "<html></html>", run_key=RUN_KEY, sensitivity=Sensitivity.CLIENT_CONFIDENTIAL
        )


@pytest.mark.unit
def test_from_static_takes_a_real_screenshot_only_when_synthetic() -> None:
    with pytest.raises(CaptureError, match="BLANK_PNG"):
        Evidence.from_static(
            "<html></html>",
            run_key=RUN_KEY,
            sensitivity=Sensitivity.PUBLIC,
            screenshot=b"\x89PNG-real",
        )
    ev = Evidence.from_static(
        "<html></html>",
        run_key=RUN_KEY,
        sensitivity=Sensitivity.SYNTHETIC,
        screenshot=b"\x89PNG-real",
    )
    assert ev.screenshot.value == b"\x89PNG-real"


@pytest.mark.unit
def test_from_static_refuses_a_credential_input_it_could_not_mask() -> None:
    with pytest.raises(CaptureError, match="credential"):
        Evidence.from_static(
            '<html><body><input type="password" value="hunter2"></body></html>',
            run_key=RUN_KEY,
            sensitivity=Sensitivity.SYNTHETIC,
        )
