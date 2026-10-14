"""Task 33: scrub -> redact -> verify -> guard -> write, and nothing written on any failure.

Adaptation (Task 19 R6/S-M3 is binding): Evidence.from_static refuses CLIENT_CONFIDENTIAL, so a
client capture is built here the way production builds one -- capture() over a Capturable
whose label is CLIENT_CONFIDENTIAL -- not with from_static as the brief's first draft did.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from bok_core.boundary.egress import Plane
from bok_core.boundary.redact import RedactionResult
from bok_core.boundary.sensitivity import Sensitivity, SensitivityViolation

from selector_drift.config import get_settings
from selector_drift.heallog import evidence as evidence_mod
from selector_drift.heallog.bundle import read_heal_manifest
from selector_drift.heallog.capture import BLANK_PNG, VIEWPORT, BBox, Evidence, capture
from selector_drift.heallog.evidence import PiiMasker, RedactionFailed, store

KEY = b"test-only-redaction-key-not-a-secret!"  # a test constant, not a credential
SESSION_COOKIE_VALUE = "sid-7f3e9a1c"
CSRF_TOKEN = "csrf-51b2c0d4"
RUN_KEY = "client-portal:1:2026-09-07"
CC = Sensitivity.CLIENT_CONFIDENTIAL

LOGGED_IN = f"""<html><head><meta name="csrf-token" content="{CSRF_TOKEN}"></head><body>
<pre data-sd-node="n1">Cookie: session={SESSION_COOKIE_VALUE}</pre>
<form><input data-sd-node="n2" type="hidden" name="authenticity_token" value="{CSRF_TOKEN}">
</form><table><tr><td data-sd-node="n3">AB12345678</td></tr></table></body></html>"""
LOGIN_PAGE = """<html><body><form><input data-sd-node="n1" name="username" value="demo">
<input data-sd-node="n2" type="password" name="password" value="hunter2"></form></body></html>"""
WITH_PII = """<html><body><table><tr><td data-sd-node="n1">AB12345678</td>
<td data-sd-node="n2">Jane Okafor</td><td data-sd-node="n3">jane.okafor@example.invalid</td>
</tr></table></body></html>"""


@dataclass
class ClientPage:
    """A Capturable labelled CLIENT_CONFIDENTIAL, as LiveCapture.of() labels a client portal."""

    html: str
    sensitivity: Sensitivity = CC

    @property
    def viewport(self) -> tuple[int, int]:
        return VIEWPORT

    def dom(self) -> str:
        return self.html

    async def masked_screenshot(
        self, *, mask_node_ids: frozenset[str], sensitivity: Sensitivity
    ) -> bytes:
        return BLANK_PNG

    async def bbox(self, node_id: str) -> BBox | None:
        return None


def _nothing(dom: str) -> frozenset[str]:
    return frozenset()


def _ev(html: str, sensitivity: Sensitivity, *, masked: bool = True) -> Evidence:
    masker = PiiMasker(KEY, sensitivity) if masked else _nothing
    if sensitivity is CC:
        return asyncio.run(capture(ClientPage(html), run_key=RUN_KEY, masker=masker))
    return Evidence.from_static(
        html, run_key=RUN_KEY, sensitivity=sensitivity, masker=masker if masked else None
    )


def bundle_bytes(uri: str) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(Path(uri).rglob("*")) if p.is_file())


def was_redacted(uri: str) -> bool:
    red = read_heal_manifest(Path(uri)).redaction
    return red.verified and red.redacted_identifiers > 0


@pytest.fixture
def evidence_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    root = tmp_path / "evidence"
    monkeypatch.setenv("SD_EVIDENCE_ROOT", str(root))
    monkeypatch.setenv("SD_REDACTION_KEY", KEY.hex())
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.mark.unit
def test_redaction_failure_writes_nothing(evidence_root: Path) -> None:
    # Un-redacted evidence is never written to be cleaned up later. There is no later.
    leaky = _ev(WITH_PII, CC, masked=False)
    with pytest.raises(RedactionFailed):
        store(leaky, Plane.CLIENT, target="client-portal", heal_id=1)
    assert not list(evidence_root.rglob("*"))


@pytest.mark.unit
def test_an_unverified_redaction_writes_nothing(
    evidence_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Adaptation: the evidence is captured BEFORE redact() is patched -- the PiiMasker calls
    # redact() too, and capture() would (rightly) wrap its failure as a CaptureError.
    ev = _ev(LOGIN_PAGE, CC)
    monkeypatch.setattr(
        evidence_mod,
        "redact",
        lambda payload, *, key: RedactionResult(
            text=payload.value, verified=False, surrogate_count={}, failures=["email"]
        ),
    )
    with pytest.raises(RedactionFailed, match="email"):
        store(ev, Plane.CLIENT, target="client-portal", heal_id=1)
    assert not list(evidence_root.rglob("*"))


@pytest.mark.unit
def test_no_session_cookie_survives_into_stored_evidence(evidence_root: Path) -> None:
    # R15/H6. A portal DOM snapshot is taken while authenticated.
    uri = store(_ev(LOGGED_IN, CC), Plane.CLIENT, target="client-portal", heal_id=1)
    blob = bundle_bytes(uri)
    assert SESSION_COOKIE_VALUE.encode() not in blob
    assert CSRF_TOKEN.encode() not in blob


@pytest.mark.unit
def test_a_password_field_value_never_reaches_the_snapshot(evidence_root: Path) -> None:
    uri = store(_ev(LOGIN_PAGE, CC), Plane.CLIENT, target="client-portal", heal_id=1)
    assert b"hunter2" not in bundle_bytes(uri)


@pytest.mark.unit
def test_client_confidential_evidence_cannot_be_written_to_the_demo_plane(
    evidence_root: Path,
) -> None:
    # Contract 1.5. This is the check guard_write exists to make, and the reason
    # it is not a bespoke local guard.
    with pytest.raises(SensitivityViolation):
        store(_ev(LOGIN_PAGE, CC), Plane.DEMO, target="client-portal", heal_id=1)
    assert not [p for p in evidence_root.rglob("*") if p.is_file()]


@pytest.mark.unit
def test_stored_evidence_contains_no_original_identifier(evidence_root: Path) -> None:
    # Fixed 2026-09-26: the second argument is a Plane, as in every other test.
    uri = store(_ev(WITH_PII, CC), Plane.CLIENT, target="client-portal", heal_id=1)
    blob = bundle_bytes(uri)
    assert b"jane.okafor@example.invalid" not in blob
    assert b"AB12345678" not in blob


@pytest.mark.unit
@pytest.mark.xfail(
    strict=True,
    reason="bok-core does not detect person names (redact.NOT_COVERED); declared in "
    "docs/methodology.md. Strict: when it does, this XPASSes and the declaration must go.",
)
def test_a_person_name_does_not_survive_into_stored_evidence(evidence_root: Path) -> None:
    uri = store(_ev(WITH_PII, CC), Plane.CLIENT, target="client-portal", heal_id=1)
    assert b"Jane Okafor" not in bundle_bytes(uri)


@pytest.mark.unit
def test_the_person_name_gap_is_declared() -> None:
    from bok_core.boundary.redact import NOT_COVERED

    text = Path("docs/methodology.md").read_text(encoding="utf-8").lower()
    assert "person_name" in NOT_COVERED
    assert "person names" in text and "addresses" in text


@pytest.mark.unit
def test_synthetic_evidence_is_still_redacted_before_storage(evidence_root: Path) -> None:
    # Design 8.2 says a SYNTHETIC target's evidence is publishable with no redaction
    # argument -- true, and exactly why redaction must NOT be conditional on the label (H7).
    uri = store(_ev(WITH_PII, Sensitivity.SYNTHETIC), Plane.DEMO, target="testbench", heal_id=1)
    assert uri and was_redacted(uri)


@pytest.mark.unit
def test_an_oversized_dom_is_refused_not_truncated(
    evidence_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev = _ev(LOGIN_PAGE, CC)
    monkeypatch.setattr(evidence_mod, "MAX_HTML_BYTES", 64)
    with pytest.raises(RedactionFailed, match="size cap"):
        store(ev, Plane.CLIENT, target="client-portal", heal_id=1)
    assert not list(evidence_root.rglob("*"))
