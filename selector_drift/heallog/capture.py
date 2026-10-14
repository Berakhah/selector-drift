"""The page the resolver saw, labelled where it is opened (INTERFACE-CONTRACT 1.1).

heallog/ may not import browser/ (heallog-cannot-refetch). A live page reaches this module
only through the Capturable protocol, which browser/live_capture.py satisfies structurally,
so the import arrow points browser -> heallog.

On EVERY capture, whatever Masker is passed (CLAUDE.md never-rules 4 and 6):
- The label is the page's own -- LiveCapture takes it from the AuthorisedTarget -- and a
  caller may only raise it, never lower it.
- The stored DOM goes through heallog.scrub.scrub_dom: credentials removed at every label,
  every form value removed at CLIENT_CONFIDENTIAL. See that module for exactly what it does
  and does not remove; PII redaction is Task 33's.
- Every credential control (one predicate, shared with the scrubber) is unioned into the
  screenshot's mask set; the masker can only add to it. A credential control with no node id
  cannot be named in the mask set, so the capture is refused. The live side of the mask --
  default-deny for CLIENT_CONFIDENTIAL, stale-id refusal -- is LiveCapture's.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import NoReturn, Protocol, SupportsIndex

from bok_core.boundary.sensitivity import Labelled, Sensitivity
from pydantic import BaseModel, ConfigDict

from selector_drift.heallog.scrub import (
    credential_node_ids,
    form_control_node_ids,
    scrub_dom_counted,
)

# One definition (Task 13). `as NODE_ATTR` is the explicit re-export mypy --strict requires, so
# `from selector_drift.heallog.capture import NODE_ATTR` (Tasks 33, 34C) keeps working.
from selector_drift.resolve.page import NODE_ATTR as NODE_ATTR

VIEWPORT: tuple[int, int] = (1280, 900)
# A bound on how many nodes one screenshot may mask by id; past it the capture is refused.
# Defined here, not in browser/, so the PII masker (Task 33) can stay under it (S-M5).
MAX_MASKED_NODES = 2_000
# Node ids are minted by the PageSource (Task 13): structural paths or short tokens.
# No quote, backslash, backtick, dollar, space or brace can appear, so an id can never
# break out of the attribute selector LiveCapture builds from it.
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:/\[\]-]{1,256}$")


class CaptureError(Exception):
    pass


class BBox(BaseModel):
    """CSS px in the full-page screenshot's coordinate space."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    x: float
    y: float
    width: float
    height: float


class Capturable(Protocol):
    @property
    def viewport(self) -> tuple[int, int]: ...

    @property
    def sensitivity(self) -> Sensitivity: ...

    def dom(self) -> str: ...

    # Named masked_screenshot, not screenshot: the only `.screenshot(` call in the product is
    # LiveCapture's own (tests/test_screenshot_boundary.py).
    # `sensitivity` is the capture's effective label (the page's, possibly raised by the
    # caller); the pixel mask must be at least as strict as the label the evidence carries.
    async def masked_screenshot(
        self, *, mask_node_ids: frozenset[str], sensitivity: Sensitivity
    ) -> bytes: ...

    async def bbox(self, node_id: str) -> BBox | None: ...


class Masker(Protocol):
    def __call__(self, dom: str) -> frozenset[str]: ...


def _png_1x1() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)  # 1x1, 8-bit greyscale
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00\x00"))
        + chunk(b"IEND", b"")
    )


BLANK_PNG = _png_1x1()


def _credentials(dom: str) -> tuple[frozenset[str], int]:
    try:
        return credential_node_ids(dom)
    except ValueError as exc:
        raise CaptureError("the captured DOM could not be parsed") from exc


def credential_input_nodes(dom: str) -> frozenset[str]:
    """Every control whose value is, or may be, a credential (R15/H6): the screenshot floor.
    The predicate is heallog.scrub's -- the one the stored DOM is scrubbed with."""
    return _credentials(dom)[0]


def _scrubbed(dom: str, sensitivity: Sensitivity) -> tuple[str, int]:
    try:
        return scrub_dom_counted(dom, sensitivity=sensitivity)
    except ValueError as exc:
        raise CaptureError("the captured DOM could not be parsed") from exc


def _live_mask_ids(dom: str, mask: frozenset[str], label: Sensitivity) -> frozenset[str]:
    """N-M4. At CLIENT_CONFIDENTIAL the live pass masks every form control by itself, so the
    page is not asked to find those by id -- a control re-rendered since the snapshot (its id
    stamp gone) is then masked rather than refused. They stay in Evidence.masked_node_ids: the
    screenshot did mask them."""
    if label is not Sensitivity.CLIENT_CONFIDENTIAL:
        return mask
    try:
        return mask - form_control_node_ids(dom)
    except ValueError as exc:
        raise CaptureError("the captured DOM could not be parsed") from exc


def _refuse_unmaskable(unmaskable: int) -> None:
    if unmaskable:
        raise CaptureError(
            f"{unmaskable} credential control(s) carry no {NODE_ATTR}; a screenshot could not "
            "mask them, so nothing is captured"
        )


def raised(page_label: Sensitivity, requested: Sensitivity | None) -> Sensitivity:
    """The caller may raise the page's label, never lower it (ordering is `.rank`)."""
    if requested is None or requested.rank <= page_label.rank:
        return page_label
    return requested


def dom_sha256(dom: str) -> str:
    return hashlib.sha256(dom.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Evidence:
    run_key: str
    dom: Labelled[str]
    screenshot: Labelled[bytes]
    captured_at: datetime
    sha256: str  # of dom.value; DomSource.snapshot_id (Task 27)
    viewport: tuple[int, int]
    masked_node_ids: frozenset[str]
    node_ids: Mapping[str, str] = field(default_factory=dict)  # field -> node_id
    boxes: Mapping[str, BBox] = field(default_factory=dict)  # field -> recorded box
    scrubbed: int = 0  # removals the capture-time scrub made (Task 33's manifest records it)

    # S-M2. Evidence is never pickled, copied or persisted as a Prefect parameter or result:
    # every such path rebuilds it through __reduce_ex__, and a persisted copy would carry the
    # DOM outside the evidence store's redaction. (dataclasses.asdict still unwraps the label;
    # tests/heallog/test_evidence_boundary.py keeps Evidence out of every flow and task.)
    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("Evidence is never pickled, copied or persisted outside heallog.evidence")

    def __post_init__(self) -> None:
        # Never-rule 6: neither half of a capture exists unlabelled, even transiently, and the
        # two halves of one capture carry one label.
        if not (isinstance(self.dom, Labelled) and isinstance(self.dom.value, str)):
            raise TypeError("Evidence.dom must be a Labelled[str]")
        if not (isinstance(self.screenshot, Labelled) and isinstance(self.screenshot.value, bytes)):
            raise TypeError("Evidence.screenshot must be a Labelled[bytes]")
        if self.dom.sensitivity is not self.screenshot.sensitivity:
            raise ValueError(
                f"Evidence halves carry different sensitivity labels "
                f"({self.dom.sensitivity} vs {self.screenshot.sensitivity})"
            )

    @classmethod
    def from_static(
        cls,
        html: str,
        *,
        run_key: str,
        sensitivity: Sensitivity,
        masker: Masker | None = None,
        screenshot: bytes = BLANK_PNG,
        captured_at: datetime | None = None,
    ) -> Evidence:
        """For fixtures and the mutation suite (Task 26). Never used on a live run, so it may
        not claim a CLIENT_CONFIDENTIAL capture, nor masking it never applied to real pixels."""
        if sensitivity is Sensitivity.CLIENT_CONFIDENTIAL:
            raise CaptureError(
                "from_static cannot produce CLIENT_CONFIDENTIAL evidence; capture() a live page"
            )
        if screenshot != BLANK_PNG and sensitivity is not Sensitivity.SYNTHETIC:
            raise CaptureError(
                "from_static takes a real screenshot only for SYNTHETIC; use BLANK_PNG"
            )
        credentials, unmaskable = _credentials(html)
        _refuse_unmaskable(unmaskable)
        mask = credentials | (_call_masker(masker, html) if masker else frozenset())
        dom, scrubbed = _scrubbed(html, sensitivity)
        return cls(
            run_key=run_key,
            dom=Labelled(value=dom, sensitivity=sensitivity),
            screenshot=Labelled(value=screenshot, sensitivity=sensitivity),
            captured_at=captured_at or datetime.now(UTC),
            sha256=dom_sha256(dom),
            viewport=VIEWPORT,
            masked_node_ids=mask,
            scrubbed=scrubbed,
        )


def _call_masker(masker: Masker, dom: str) -> frozenset[str]:
    """S-M1: the masker reads the raw DOM, so its exception text may quote it. Neither the
    message nor the cause reaches the caller (or an alert)."""
    try:
        return frozenset(masker(dom))
    except Exception:
        raise CaptureError(f"masker {type(masker).__name__} failed") from None


async def capture(
    page: Capturable,
    *,
    run_key: str,
    masker: Masker,
    sensitivity: Sensitivity | None = None,
) -> Evidence:
    """`sensitivity` is a floor the caller may add; the label is the page's own (R3)."""
    if tuple(page.viewport) != VIEWPORT:
        raise CaptureError(
            f"viewport {page.viewport} is not the fixed {VIEWPORT}; "
            "recorded boxes would not be comparable across runs"
        )
    label = raised(page.sensitivity, sensitivity)
    raw = page.dom()
    credentials, unmaskable = _credentials(raw)
    _refuse_unmaskable(unmaskable)
    # The floor is unioned in, never replaced: a masker can only add.
    mask = _call_masker(masker, raw) | credentials
    live = _live_mask_ids(raw, mask, label)
    dom, scrubbed = _scrubbed(raw, label)
    del raw  # S-M1: drop the unscrubbed DOM before anything else can hold it
    bad = sum(1 for n in mask if not NODE_ID_RE.fullmatch(n))
    if bad:
        raise CaptureError(f"{bad} node id(s) to mask are not minted node ids")
    shot = await page.masked_screenshot(mask_node_ids=live, sensitivity=label)
    return Evidence(
        run_key=run_key,
        dom=Labelled(value=dom, sensitivity=label),
        screenshot=Labelled(value=shot, sensitivity=label),
        captured_at=datetime.now(UTC),
        sha256=dom_sha256(dom),
        viewport=VIEWPORT,
        masked_node_ids=mask,
        scrubbed=scrubbed,
    )


async def record_boxes(
    evidence: Evidence, page: Capturable, resolved: Mapping[str, str]
) -> Evidence:
    """Record each resolved node's box from the live page, before it closes (API spec 7.2)."""
    for field_name, node_id in resolved.items():
        if not NODE_ID_RE.fullmatch(node_id):
            raise CaptureError(f"node id for {field_name!r} is not a minted node id")
    boxes: dict[str, BBox] = {}
    for field_name, node_id in resolved.items():
        box = await page.bbox(node_id)
        if box is not None:
            boxes[field_name] = box
    return replace(evidence, node_ids=dict(resolved), boxes=boxes)
