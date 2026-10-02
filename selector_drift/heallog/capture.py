"""The page the resolver saw, labelled where it is opened (INTERFACE-CONTRACT 1.1).

heallog/ may not import browser/ (heallog-cannot-refetch). A live page reaches this module
only through the Capturable protocol, which browser/live_capture.py satisfies structurally,
so the import arrow points browser -> heallog.

The credential floor (CLAUDE.md never-rule 4) is applied on EVERY capture, whatever Masker is
passed: every credential input is unioned into the screenshot's mask set, and its value is
dropped from the stored DOM. A credential input that carries no node id cannot be named in
the mask set, so the capture is refused rather than emitted with the field visible. The
masker's own set (Task 33's PiiMasker) only ever adds to the floor.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol

import lxml.html
from bok_core.boundary.sensitivity import Labelled, Sensitivity
from lxml import etree
from lxml.html import HtmlElement
from pydantic import BaseModel, ConfigDict

# One definition (Task 13). `as NODE_ATTR` is the explicit re-export mypy --strict requires, so
# `from selector_drift.heallog.capture import NODE_ATTR` (Tasks 33, 34C) keeps working.
from selector_drift.resolve.page import NODE_ATTR as NODE_ATTR

VIEWPORT: tuple[int, int] = (1280, 900)
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

    def dom(self) -> str: ...

    async def screenshot(self, *, mask_node_ids: frozenset[str]) -> bytes: ...

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

# -- the credential floor (R15/H6, never-rule 4) -------------------------------------------
_CREDENTIAL_TYPES = frozenset({"password", "hidden"})
_CREDENTIAL_AUTOCOMPLETE = frozenset({"current-password", "new-password", "one-time-code"})
# Whole words only, so "shipping" never matches "pin" and "bypass" never matches "pass".
_CREDENTIAL_WORDS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "pass",
        "passcode",
        "passphrase",
        "otp",
        "totp",
        "hotp",
        "mfa",
        "2fa",
        "pin",
        "secret",
        "token",
        "csrf",
        "xsrf",
        "apikey",
        "credential",
        "credentials",
    }
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(_CAMEL.sub(" ", text).lower()))


def _parse(dom: str) -> HtmlElement:
    try:
        return lxml.html.document_fromstring(dom)
    except (etree.ParserError, ValueError) as exc:
        raise CaptureError("the captured DOM could not be parsed") from exc


class _Labels:
    """Label text by target id and element text by id, indexed once per DOM -- so finding
    credentials is one pass over the page, not one pass per input."""

    def __init__(self, root: HtmlElement) -> None:
        self.label_for: dict[str, list[str]] = {}
        self.by_id: dict[str, str] = {}
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            if el.tag == "label" and el.get("for"):
                self.label_for.setdefault(str(el.get("for")), []).append(el.text_content())
            ident = el.get("id")
            if ident and ident not in self.by_id:
                self.by_id[ident] = el.text_content()

    def text_for(self, el: HtmlElement) -> str:
        parts = list(self.label_for.get(el.get("id") or "", []))
        parts.extend(a.text_content() for a in el.iterancestors("label"))
        parts.extend(self.by_id.get(ref, "") for ref in (el.get("aria-labelledby") or "").split())
        return " ".join(parts)


def _is_credential(el: HtmlElement, labels: _Labels) -> bool:
    kind = (el.get("type") or "text").strip().lower()
    if el.tag == "input" and kind in _CREDENTIAL_TYPES:
        return True
    if set((el.get("autocomplete") or "").lower().split()) & _CREDENTIAL_AUTOCOMPLETE:
        return True
    described = " ".join(
        el.get(attr) or "" for attr in ("name", "id", "aria-label", "placeholder", "title")
    )
    return bool(_words(f"{described} {labels.text_for(el)}") & _CREDENTIAL_WORDS)


def _credential_elements(root: HtmlElement) -> Iterator[HtmlElement]:
    candidates = [el for el in root.iter("input", "textarea") if isinstance(el, HtmlElement)]
    if not candidates:
        return
    labels = _Labels(root)
    for el in candidates:
        if _is_credential(el, labels):
            yield el


def credential_input_nodes(dom: str) -> frozenset[str]:
    """Every input whose value is, or may be, a credential (R15/H6): password and hidden
    inputs, credential autocomplete tokens, and inputs named or labelled like a password, OTP,
    PIN, secret or token. Returns their node ids; see _unmaskable for those without one."""
    root = _parse(dom)
    return frozenset(
        str(el.get(NODE_ATTR)) for el in _credential_elements(root) if el.get(NODE_ATTR)
    )


def _scrub_credentials(dom: str) -> tuple[str, int]:
    """The DOM with every credential input's value dropped, and how many lacked a node id.

    Re-serialised only when something was actually removed, so a page with no credential
    values is stored byte-for-byte as the resolver saw it."""
    root = _parse(dom)
    changed, unmaskable = False, 0
    for el in _credential_elements(root):
        if not el.get(NODE_ATTR):
            unmaskable += 1
        if el.get("value") is not None:
            el.set("value", "")
            changed = True
        if el.tag == "textarea" and (el.text or len(el)):
            for child in list(el):
                el.remove(child)
            el.text = None
            changed = True
    if not changed:
        return dom, unmaskable
    docinfo = root.getroottree().docinfo
    doctype = docinfo.doctype or None
    return lxml.html.tostring(root, encoding="unicode", doctype=doctype), unmaskable


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

    def __post_init__(self) -> None:
        # Never-rule 6: neither half of a capture exists unlabelled, even transiently.
        if not (isinstance(self.dom, Labelled) and isinstance(self.dom.value, str)):
            raise TypeError("Evidence.dom must be a Labelled[str]")
        if not (isinstance(self.screenshot, Labelled) and isinstance(self.screenshot.value, bytes)):
            raise TypeError("Evidence.screenshot must be a Labelled[bytes]")

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
        """For fixtures and the mutation suite (Task 26). Never used on a live run."""
        mask = credential_input_nodes(html) | (masker(html) if masker else frozenset())
        dom, _ = _scrub_credentials(html)
        return cls(
            run_key=run_key,
            dom=Labelled(value=dom, sensitivity=sensitivity),
            screenshot=Labelled(value=screenshot, sensitivity=sensitivity),
            captured_at=captured_at or datetime.now(UTC),
            sha256=dom_sha256(dom),
            viewport=VIEWPORT,
            masked_node_ids=mask,
        )


async def capture(
    page: Capturable, *, run_key: str, sensitivity: Sensitivity, masker: Masker
) -> Evidence:
    if tuple(page.viewport) != VIEWPORT:
        raise CaptureError(
            f"viewport {page.viewport} is not the fixed {VIEWPORT}; "
            "recorded boxes would not be comparable across runs"
        )
    raw = page.dom()
    dom, unmaskable = _scrub_credentials(raw)
    if unmaskable:
        raise CaptureError(
            f"{unmaskable} credential input(s) carry no {NODE_ATTR}; the screenshot could not "
            "mask them, so nothing is captured"
        )
    # The floor is unioned in, never replaced: a masker can only add.
    mask = frozenset(masker(raw)) | credential_input_nodes(raw)
    bad = sorted(n for n in mask if not NODE_ID_RE.fullmatch(n))
    if bad:
        raise CaptureError(f"{len(bad)} node id(s) to mask are not minted node ids")
    shot = await page.screenshot(mask_node_ids=mask)
    return Evidence(
        run_key=run_key,
        dom=Labelled(value=dom, sensitivity=sensitivity),
        screenshot=Labelled(value=shot, sensitivity=sensitivity),
        captured_at=datetime.now(UTC),
        sha256=dom_sha256(dom),
        viewport=VIEWPORT,
        masked_node_ids=mask,
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
