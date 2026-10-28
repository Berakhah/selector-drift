"""The evidence write path: capture -> scrub credentials -> redact -> verify -> egress guard ->
write (INTERFACE-CONTRACT 1.3, 1.5; API spec 7.2). CLAUDE.md never-rules 4 and 6.

Two stores, one write path. `last-accepted/<target>/` is replaced whole on every accepted run;
`heals/<heal_id>/` is written once. Redaction is its own function, `redact_evidence`, run in
memory BEFORE anything is written: a failure leaves both stores untouched, and there is no
"clean it up later".

What redact_evidence does, in order, unconditionally (SYNTHETIC evidence too, H7):
1. Refuses a DOM over bundle.MAX_HTML_BYTES -- never truncated (bok-core pointer 2).
2. Re-runs THE one scrubber (heallog.scrub.scrub_dom, Task 19 R5) at the evidence's own label:
   credentials do not look like PII, so redact() would miss every one of them (H6). capture()
   already scrubbed; this pass is idempotent and its count is added to the capture's.
3. bok-core redact() over the VALUES of the scrubbed tree -- every text, tail and attribute
   value (never the minted node ids), batched -- then over each block's rendered run, so an
   identifier split by markup or entities is redacted whole (fix round 1, C1 + I1). Markup is
   never handed to the redactor. Verified by re-detecting over the result's values and runs,
   and by requiring the serialised page to be a redact() fixpoint (that result is the proof
   the guard re-checks). An unverified pass, or one past Settings.redaction_deadline_s,
   raises RedactionFailed.
4. The screenshot is verified STRUCTURALLY (a PNG has no text to re-detect): every element
   whose rendered text (or value/placeholder/alt) the redactor would change, and every
   credential control, must have itself or an ancestor in the capture's mask set -- else
   RedactionFailed("... not masked"). Then every PNG chunk that is not image data is dropped.

What is written, and under which label (contract 1.5; bok-core pointer 5):
- The HTML of a CLIENT_CONFIDENTIAL (or REDACTED) capture goes to write_guarded labelled
  REDACTED WITH its RedactionResult, so the guard re-runs every detector over the exact bytes
  and refuses any plane but CLIENT. A SYNTHETIC/PUBLIC capture's HTML keeps its own label (a
  REDACTED label would be refused on DEMO); its redaction was verified in step 3.
- The PNG and the manifest carry the capture's own label: a screenshot cannot be REDACTED, so a
  client screenshot is CLIENT_CONFIDENTIAL and the guard keeps it on the CLIENT plane.
- The manifest holds only system-minted values (run key, node ids, field names, boxes,
  counts) -- never page text (bok-core pointer 1); each is validated before it is written.

Paths (S-M4; bok-core pointer 6): every path is derived here from an integer heal id or a
validated target name under the one storage root, never from a stored or requested string. A
target's last-accepted directory is an opaque keyed name, so no client identifier appears in a
path. Writes refuse any link on the way into the store.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import struct
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, NoReturn, SupportsIndex

import lxml.html
from bok_core.boundary.egress import Plane, write_guarded
from bok_core.boundary.redact import RedactionResult, redact
from bok_core.boundary.sensitivity import Labelled, Sensitivity
from lxml.html import HtmlElement
from sqlalchemy import select

from selector_drift.config import get_settings
from selector_drift.db.models import ScopeRecordRow
from selector_drift.db.session import Session
from selector_drift.heallog.bundle import (
    AFTER_HTML,
    AFTER_PNG,
    BEFORE_HTML,
    BEFORE_PNG,
    HEALS_DIR,
    LAST_ACCEPTED_DIR,
    MANIFEST,
    MAX_HTML_BYTES,
    MAX_PNG_BYTES,
    PAGE_HTML,
    PAGE_PNG,
    EvidenceCorrupted,
    HealManifest,
    NodeBox,
    PageManifest,
    RedactionBlock,
    SideBlock,
    file_digest,
    is_link,
    read_capped,
    read_heal_manifest,
    read_page_manifest,
    verified_bytes,
)
from selector_drift.heallog.capture import (
    MAX_MASKED_NODES,
    NODE_ATTR,
    NODE_ID_RE,
    BBox,
    Evidence,
)
from selector_drift.heallog.scrub import credential_controls, parse, scrub_dom_counted
from selector_drift.resolve.cascade import parse_run_key
from selector_drift.scope.errors import ScopeRefusal
from selector_drift.scope.record import Basis
from selector_drift.spec.loader import load_scope, scope_file

_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")  # scope.record.TARGET_SLUG
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_KEEP = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS"})
# A separator no detector can match across ('☃' is in no detector's character class and
# survives normalisation), so one redact() call serves every element of a page.
_SEP = "\n☃\n"
_SURROGATE = re.compile(r"\[(?:EMAIL|IBAN|PHONE|DIGIT_RUN|CARD|TAX_ID|ROUTING|ACCOUNT):[a-p]{12}\]")
_UNRENDERED = frozenset({"script", "style", "noscript", "template", "head", "title", "meta"})
# Elements that start a new rendered box: text is never joined across them (I1 runs).
_BLOCKS = frozenset(
    {"address", "article", "aside", "blockquote", "body", "br", "button", "caption", "dd",
     "details", "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
     "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "html", "iframe", "img",
     "input", "label", "legend", "li", "main", "nav", "ol", "option", "p", "pre", "section",
     "select", "summary", "table", "tbody", "td", "textarea", "tfoot", "th", "thead", "tr",
     "ul"}
)  # fmt: skip
_RENDERED_ATTRS = ("value", "placeholder", "alt")
# S-M5: the PII mask must leave room for the credential floor under MAX_MASKED_NODES.
PII_MASK_BUDGET = MAX_MASKED_NODES // 2
_SECRET_LABELS = frozenset({Sensitivity.CLIENT_CONFIDENTIAL, Sensitivity.REDACTED})


class RedactionFailed(Exception):
    """The caller quarantines the run with reason 'evidence redaction failed'."""


class UnsafeEvidencePath(ValueError):
    """A link, or a path outside the store, on the way to an evidence write."""


# ----------------------------------------------------------------- scrub, strip, mask


def scrub_credentials(
    dom: str, *, sensitivity: Sensitivity = Sensitivity.CLIENT_CONFIDENTIAL
) -> tuple[str, int]:
    """R15/H6, through the ONE scrubber (heallog.scrub). Returns the scrubbed DOM and how many
    removals it made. The default label is the strictest (default-deny on form values)."""
    try:
        return scrub_dom_counted(dom, sensitivity=sensitivity)
    except ValueError as exc:
        raise RedactionFailed("the DOM could not be parsed") from exc


def strip_png_metadata(png: bytes) -> bytes:
    """Keep image data only. A screenshot tool can write a URL -- or a cookie -- into tEXt."""
    if not png.startswith(_PNG_SIG):
        raise RedactionFailed("screenshot is not a PNG")
    if len(png) > MAX_PNG_BYTES:
        raise RedactionFailed("screenshot is over its size cap")
    out, pos, first = bytearray(_PNG_SIG), len(_PNG_SIG), True
    ended = False
    while pos < len(png):
        if pos + 12 > len(png):
            raise RedactionFailed("screenshot PNG is truncated")
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        tag, end = png[pos + 4 : pos + 8], pos + 12 + length
        if end > len(png):
            raise RedactionFailed("screenshot PNG chunk is truncated")
        if first and tag != b"IHDR":
            raise RedactionFailed("screenshot PNG does not start with IHDR")
        first = False
        if tag in _PNG_KEEP:
            out += png[pos:end]
        pos = end
        if tag == b"IEND":
            ended = True
            break
    if not ended:
        raise RedactionFailed("screenshot PNG has no IEND")
    return bytes(out)


@dataclass(frozen=True)
class _Slot:
    """One value in the tree: an element's text, its tail, or one attribute's value."""

    el: HtmlElement
    kind: str  # "text" | "tail" | "attr"
    name: str = ""

    def get(self) -> str:
        if self.kind == "attr":
            return str(self.el.get(self.name) or "")
        return str(getattr(self.el, self.kind) or "")

    def put(self, value: str) -> None:
        if self.kind == "attr":
            self.el.set(self.name, value)
        else:
            setattr(self.el, self.kind, value or None)

    def owner(self) -> HtmlElement | None:
        """The element that paints this value (a tail renders in its parent)."""
        if self.kind == "tail":
            parent = self.el.getparent()
            return parent if isinstance(parent, HtmlElement) else None
        return self.el


def _elements_of(root: HtmlElement) -> Iterator[HtmlElement]:
    for el in root.iter():
        if isinstance(el, HtmlElement) and isinstance(el.tag, str):
            yield el


def _value_slots(root: HtmlElement) -> list[_Slot]:
    """Every text, tail and attribute value -- except the minted node ids, which are not page
    content and which replay and the screenshot mask depend on."""
    slots: list[_Slot] = []
    for el in _elements_of(root):
        if el.text:
            slots.append(_Slot(el, "text"))
        if el.tail:
            slots.append(_Slot(el, "tail"))
        for name in el.attrib:
            if name != NODE_ATTR and el.get(name):
                slots.append(_Slot(el, "attr", str(name)))
    return slots


def _runs(root: HtmlElement) -> list[tuple[HtmlElement, list[_Slot]]]:
    """Each block element's rendered runs: its own text and its inline descendants' text and
    tails, in order, cut wherever a nested block starts. This is what the page PAINTS as one
    string -- "<span>4111</span> <span>1111</span>", "1234<b>5678</b>90" and
    "jane.okafor<wbr>@example.invalid" are each one run (fix round 1, I1)."""
    runs: list[tuple[HtmlElement, list[_Slot]]] = []

    def walk_block(block: HtmlElement) -> None:
        current: list[_Slot] = []

        def flush() -> None:
            nonlocal current
            if current:
                runs.append((block, current))
                current = []

        def inline(el: HtmlElement) -> None:
            if el.text:
                current.append(_Slot(el, "text"))
            for child in el:
                if isinstance(child, HtmlElement) and isinstance(child.tag, str):
                    if child.tag in _UNRENDERED:
                        pass
                    elif child.tag in _BLOCKS:
                        flush()
                        walk_block(child)
                    else:
                        inline(child)
                if child.tail:
                    current.append(_Slot(child, "tail"))

        inline(block)
        flush()

    body = root.find("body")
    walk_block(body if isinstance(body, HtmlElement) else root)
    return runs


def _redact_texts(
    texts: list[str], *, key: bytes, sensitivity: Sensitivity
) -> tuple[list[str], int]:
    """Each text redacted, and how many identifiers were replaced. One redact() call for the
    page, joined by a separator no detector matches across; per text when a text carries the
    separator or the split does not line up. Raises RedactionFailed when unverified."""
    if not texts:
        return [], 0
    if not any("\u2603" in t for t in texts):
        # A trailing separator: the joined payload is never valid JSON, so bok-core never
        # switches to its JSON-number mode (which quotes surrogates) on a lone value.
        result = redact(Labelled(_SEP.join([*texts, ""]), sensitivity), key=key)
        if not result.verified:
            raise RedactionFailed(f"redaction verification failed: {', '.join(result.failures)}")
        parts = result.text.split(_SEP)
        if len(parts) == len(texts) + 1 and parts[-1] == "":
            return parts[:-1], sum(result.surrogate_count.values())
    out: list[str] = []
    count = 0
    for text in texts:
        result = redact(Labelled(text, sensitivity), key=key)
        if not result.verified:
            raise RedactionFailed(f"redaction verification failed: {', '.join(result.failures)}")
        out.append(result.text)
        count += sum(result.surrogate_count.values())
    return out, count


def _hits(before: list[str], after: list[str]) -> list[bool]:
    return [
        len(_SURROGATE.findall(a)) > len(_SURROGATE.findall(b))
        for b, a in zip(before, after, strict=True)
    ]


@dataclass(frozen=True)
class _TreeRedaction:
    flagged: list[HtmlElement]  # elements whose painted text carried an identifier
    identifiers: int


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise RedactionFailed(
            "evidence redaction exceeded its deadline; refused, nothing stored (never truncated)"
        )


def _redact_tree(
    root: HtmlElement,
    *,
    key: bytes,
    sensitivity: Sensitivity,
    mutate: bool,
    deadline: float | None = None,
) -> _TreeRedaction:
    """Redact VALUES, never markup (fix round 1, C1): every text, tail and attribute value is
    redacted in place, so tags, attribute names and node ids are untouched. Then every
    rendered run (I1) that still carries an identifier across slots -- an identifier split by
    markup -- is redacted as a whole: its first slot takes the redacted run, the others are
    blanked. With mutate=False nothing is changed; only the flags are computed (PiiMasker)."""
    slots = _value_slots(root)
    original = [s.get() for s in slots]
    index = {(id(s.el), s.kind, s.name): i for i, s in enumerate(slots)}
    runs = _runs(root)
    run_slots = [[index[(id(s.el), s.kind, s.name)] for s in members] for _, members in runs]
    _check_deadline(deadline)

    redacted, identifiers = _redact_texts(original, key=key, sensitivity=sensitivity)
    slot_hit = _hits(original, redacted)
    _check_deadline(deadline)

    flagged: dict[int, HtmlElement] = {}
    for i, hit in enumerate(slot_hit):
        slot = slots[i]
        rendered = slot.kind != "attr" or slot.name in _RENDERED_ATTRS
        owner = slot.owner()
        if hit and rendered and owner is not None and _is_rendered(owner):
            flagged[id(owner)] = owner

    # A run is redacted as a whole when redacting it whole differs from redacting its pieces:
    # an identifier split by markup ("4111</span> <span>1111..."), or one whose pieces each
    # matched only in part ("jane.okafor" + "@example.invalid" -- the tail alone is an
    # email with an empty local part, and the local part would survive).
    pieces = ["".join(redacted[i] for i in members) for members in run_slots]
    whole, _ = _redact_texts(
        ["".join(original[i] for i in members) for members in run_slots],
        key=key,
        sensitivity=sensitivity,
    )
    _check_deadline(deadline)
    for (block, _), members, piecewise, text in zip(runs, run_slots, pieces, whole, strict=True):
        if piecewise == text:
            continue
        flagged[id(block)] = block
        first, *rest = members
        redacted[first] = text
        for i in rest:
            redacted[i] = ""
        identifiers += 1

    if mutate:
        for slot, value in zip(slots, redacted, strict=True):
            if value != slot.get():
                slot.put(value)
    return _TreeRedaction(flagged=list(flagged.values()), identifiers=identifiers)


def _is_rendered(el: HtmlElement) -> bool:
    return el.tag not in _UNRENDERED and not any(a.tag in _UNRENDERED for a in el.iterancestors())


def _detections(root: HtmlElement, *, key: bytes, sensitivity: Sensitivity) -> int:
    """Verification over the REDACTED tree: detectors re-run over every decoded value and every
    rendered run. Any identifier found is a failure."""
    texts = [s.get() for s in _value_slots(root)]
    texts += ["".join(s.get() for s in members) for _, members in _runs(root)]
    _, found = _redact_texts(texts, key=key, sensitivity=sensitivity)
    return found


def _flagged_elements(
    root: HtmlElement, *, key: bytes, sensitivity: Sensitivity
) -> list[HtmlElement]:
    return _redact_tree(root, key=key, sensitivity=sensitivity, mutate=False).flagged


def _carrier(el: HtmlElement) -> HtmlElement | None:
    """The element itself, or its nearest ancestor, that carries a node id."""
    if el.get(NODE_ATTR):
        return el
    for anc in el.iterancestors():
        if isinstance(anc, HtmlElement) and anc.get(NODE_ATTR):
            return anc
    return None


def _minimal(elements: list[HtmlElement]) -> list[HtmlElement]:
    """Drop every element that has an ancestor in the set (masking the ancestor covers it)."""
    chosen = {id(e) for e in elements}
    out: list[HtmlElement] = []
    seen: set[int] = set()
    for e in elements:
        if id(e) in seen:
            continue
        seen.add(id(e))
        if not any(id(a) in chosen for a in e.iterancestors()):
            out.append(e)
    return out


def pii_node_ids(
    dom: str, *, key: bytes, sensitivity: Sensitivity, budget: int = PII_MASK_BUDGET
) -> frozenset[str]:
    """The node ids to mask so that no identifier the redactor would change is painted.

    Each flagged element is masked by itself or, without an id, by its nearest ancestor with
    one. S-M5: past `budget` the set is collapsed upward -- every mask is replaced by its
    nearest id-carrying ancestor (a row, then the table, ...) -- until it fits, rather than
    exceeding MAX_MASKED_NODES and refusing the capture. Coarser, never thinner."""
    try:
        root = parse(dom)
    except ValueError as exc:
        raise RedactionFailed("the DOM could not be parsed") from exc
    carriers = [
        c
        for e in _flagged_elements(root, key=key, sensitivity=sensitivity)
        if (c := _carrier(e)) is not None
    ]
    current = _minimal(carriers)
    while len(current) > budget:
        lifted = []
        moved = False
        for el in current:
            parent = el.getparent()
            up = _carrier(parent) if isinstance(parent, HtmlElement) else None
            if up is None:
                lifted.append(el)
            else:
                lifted.append(up)
                moved = True
        current = _minimal(lifted)
        if not moved:
            break
    return frozenset(str(el.get(NODE_ATTR)) for el in current)


@dataclass(frozen=True)
class PiiMasker:
    """The production Masker. It reads the raw DOM, so it never logs and never puts DOM text
    in an exception (capture() also wraps whatever it raises, S-M1)."""

    key: bytes = field(repr=False)
    sensitivity: Sensitivity

    def __call__(self, dom: str) -> frozenset[str]:
        return pii_node_ids(dom, key=self.key, sensitivity=self.sensitivity)


def _uncovered(root: HtmlElement, required: list[HtmlElement], masked: frozenset[str]) -> int:
    count = 0
    for el in required:
        chain = [el, *el.iterancestors()]
        if not any(isinstance(a, HtmlElement) and a.get(NODE_ATTR) in masked for a in chain):
            count += 1
    return count


# ----------------------------------------------------------------- redact (in memory)

_REDACTED_TOKEN = object()


@dataclass(frozen=True)
class RedactedEvidence:
    _token: object = field(repr=False, compare=False)
    run_key: str
    captured_at: datetime
    viewport: tuple[int, int]
    html: bytes = field(repr=False)
    png: bytes = field(repr=False)
    sensitivity: Sensitivity  # of the source capture; the label each file is guarded under
    redaction: RedactionResult = field(repr=False)
    scrubbed_credentials: int
    identifiers: int
    node_ids: Mapping[str, str] = field(default_factory=dict)
    boxes: Mapping[str, BBox] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self._token is not _REDACTED_TOKEN:
            raise TypeError("RedactedEvidence is minted only by redact_evidence()")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("RedactedEvidence is never pickled, copied or persisted")

    def side(self) -> SideBlock:
        return SideBlock(
            run_key=self.run_key,
            captured_at=self.captured_at,
            viewport=self.viewport,
            nodes={
                f: NodeBox(node_id=self.node_ids[f], bbox=b)
                for f, b in self.boxes.items()
                if f in self.node_ids
            },
        )

    def redaction_block(self) -> RedactionBlock:
        return RedactionBlock(
            scrubbed_credentials=self.scrubbed_credentials,
            redacted_identifiers=self.identifiers,
            verified=self.redaction.verified,
        )


def _check_minted_values(evidence: Evidence) -> None:
    """Bok-core pointer 1: the manifest is serialised unredacted, so only system-minted values
    may reach it."""
    try:
        parse_run_key(evidence.run_key)
    except ValueError:
        raise RedactionFailed("the evidence's run key is not a run key") from None
    for name, node_id in evidence.node_ids.items():
        if not _FIELD_RE.fullmatch(name) or not NODE_ID_RE.fullmatch(node_id):
            raise RedactionFailed("a recorded node is not a field name and a minted node id")
    if any(not _FIELD_RE.fullmatch(name) for name in evidence.boxes):
        raise RedactionFailed("a recorded box is not keyed by a field name")


def _serialise(root: HtmlElement, doctype: str | None) -> str:
    return str(lxml.html.tostring(root, encoding="unicode", doctype=doctype))


def redact_evidence(
    evidence: Evidence, *, key: bytes, deadline_s: float | None = None
) -> RedactedEvidence:
    """Unconditional -- SYNTHETIC evidence is redacted too (H7). Writes nothing.

    Fix round 1 (C1): values are redacted on the parsed tree and the tree re-serialised, so
    the stored page has the same element skeleton and node ids as the scrubbed one. Verified
    twice: detectors re-run over every decoded value and rendered run of the result, and the
    serialised page must be a fixpoint of redact() -- whose result is the proof the egress
    guard re-verifies. Past `deadline_s` (Settings.redaction_deadline_s) it raises
    RedactionFailed: nothing is stored, never a truncated redaction."""
    deadline = time.monotonic() + (
        get_settings().redaction_deadline_s if deadline_s is None else deadline_s
    )
    label = evidence.dom.sensitivity
    raw = evidence.dom.value
    if len(raw.encode("utf-8")) > MAX_HTML_BYTES:
        raise RedactionFailed("the DOM is over its size cap; refused, not truncated")
    _check_minted_values(evidence)
    scrubbed, n_scrubbed = scrub_credentials(raw, sensitivity=label)
    _check_deadline(deadline)
    try:
        root = parse(scrubbed)
    except ValueError:
        raise RedactionFailed("the DOM could not be parsed") from None
    doctype = root.getroottree().docinfo.doctype or None
    required = credential_controls(root)
    tree = _redact_tree(root, key=key, sensitivity=label, mutate=True, deadline=deadline)
    if _detections(root, key=key, sensitivity=label):
        raise RedactionFailed("redaction verification failed: an identifier survived in a value")
    _check_deadline(deadline)
    html = _serialise(root, doctype)
    proof = redact(Labelled(html, label), key=key)
    if not proof.verified or proof.text != html:
        raise RedactionFailed(
            "redaction verification failed: the redacted page is not a redaction fixpoint"
        )
    _check_deadline(deadline)
    missing = _uncovered(root, tree.flagged + required, evidence.masked_node_ids)
    if missing:
        raise RedactionFailed(
            f"{missing} identifier-bearing or credential element(s) were not masked in the "
            "screenshot"
        )
    return RedactedEvidence(
        _REDACTED_TOKEN,
        run_key=evidence.run_key,
        captured_at=evidence.captured_at,
        viewport=evidence.viewport,
        html=html.encode("utf-8"),
        png=strip_png_metadata(evidence.screenshot.value),
        sensitivity=label,
        redaction=proof,
        scrubbed_credentials=evidence.scrubbed + n_scrubbed,
        identifiers=tree.identifiers,
        node_ids=dict(evidence.node_ids),
        boxes=dict(evidence.boxes),
    )


# ----------------------------------------------------------------- paths


def _store_root(plane: Plane | None = None) -> Path:
    """Plane -> storage root, in this one place (bok-core pointer 6). There is one evidence
    store and it sits on the CLIENT plane (compose-only); a DEMO-asserted write of SYNTHETIC or
    PUBLIC evidence lands there too, which is the safe direction -- the CLIENT plane may hold
    DEMO material, never the reverse. Publishing anything off it is an explicit allowlist
    export, not this module."""
    if plane is not None:
        Plane(plane)  # an unknown plane is refused here as well as by the guard
    return get_settings().evidence_root


def _target_slug(target: str) -> str:
    """Fix round 1: the error never repeats the value (it may be a client's name)."""
    if not isinstance(target, str) or not _TARGET_RE.fullmatch(target):
        raise ValueError("not a target name")
    return target


PATH_KEY_FILE = ".path-key"
_PATH_KEY_BYTES = 32


def _path_key(*, create: bool) -> bytes | None:
    """The store's own key for naming last-accepted directories (fix round 1, I3).

    NOT the redaction key: rotating SD_REDACTION_KEY must not orphan a directory, and an
    operator answering a deletion request must be able to find a target's directory with
    last_accepted_dir(target). It names paths only (it protects no content); it lives at the
    store root, created once (O_EXCL, 0600). None when it does not exist and `create` is
    False -- then no last-accepted directory can exist either."""
    root = _store_root()
    path = root / PATH_KEY_FILE
    if not path.exists() and not is_link(path):
        if not create:
            return None
        root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets.token_bytes(_PATH_KEY_BYTES))
    try:
        key = read_capped(path, _PATH_KEY_BYTES)
    except (OSError, EvidenceCorrupted):
        raise UnsafeEvidencePath("the evidence store's path key is unreadable") from None
    if len(key) != _PATH_KEY_BYTES:
        raise UnsafeEvidencePath("the evidence store's path key is malformed")
    return key


def _last_accepted_name(slug: str, key: bytes) -> str:
    digest = hmac.new(key, b"sd-last-accepted\x00" + slug.encode(), hashlib.sha256).hexdigest()
    return f"t-{digest[:32]}"


def last_accepted_dir(target: str, *, create_key: bool = True) -> Path:
    """`last-accepted/t-<digest of the target under the store's path key>`: opaque, so a
    directory listing or an error naming a path never names a client (bok-core pointer 6),
    and stable across redaction-key rotation (fix round 1)."""
    slug = _target_slug(target)
    key = _path_key(create=create_key)
    name = _last_accepted_name(slug, key) if key is not None else "t-absent"
    return _store_root() / LAST_ACCEPTED_DIR / name


def last_accepted_names(targets: list[str]) -> dict[str, str]:
    """{directory name: target} for the given targets -- for retention's orphan sweep. Empty
    when the store has no path key yet."""
    key = _path_key(create=False)
    if key is None:
        return {}
    return {_last_accepted_name(_target_slug(t), key): t for t in targets}


def heal_bundle_dir(heal_id: int) -> Path:
    if isinstance(heal_id, bool) or not isinstance(heal_id, int) or heal_id <= 0:
        raise ValueError("heal ids are positive integers")
    return _store_root() / HEALS_DIR / str(heal_id)


def _no_links(final: Path) -> None:
    """Every directory from the store root down to `final` exists as a real directory (or
    not yet), never a link, and `final` resolves inside the root."""
    root = _store_root()
    root.mkdir(parents=True, exist_ok=True)
    if is_link(root):
        raise UnsafeEvidencePath("the evidence root is a link")
    current = root
    for part in final.relative_to(root).parts:
        current = current / part
        if is_link(current):
            raise UnsafeEvidencePath(f"{current.name} on the evidence path is a link")
    final.parent.mkdir(parents=True, exist_ok=True)
    if not final.parent.resolve().is_relative_to(root.resolve()):
        raise UnsafeEvidencePath("the evidence path leaves the store")


# ----------------------------------------------------------------- atomic, guarded writes


@dataclass(frozen=True)
class _File:
    name: str
    payload: Labelled[bytes] = field(repr=False)
    proof: RedactionResult | None = field(default=None, repr=False)


def _html_file(name: str, html: bytes, sensitivity: Sensitivity, proof: RedactionResult) -> _File:
    if sensitivity in _SECRET_LABELS:
        return _File(name, Labelled(html, Sensitivity.REDACTED), proof)
    return _File(name, Labelled(html, sensitivity))


def _write_dir(
    final: Path,
    files: list[_File],
    sensitivity: Sensitivity,
    plane: Plane,
    manifest: bytes,
    *,
    replace_existing: bool,
) -> None:
    """Assemble in a sibling `.tmp-<token>/`, every file through write_guarded, then rename.
    A reader never sees half a bundle; a failure leaves nothing."""
    _no_links(final)
    if final.exists() and not replace_existing:
        raise FileExistsError(f"heal bundle {final.name} is written once")
    tmp = final.parent / f".tmp-{secrets.token_hex(13)}"
    tmp.mkdir()
    try:
        for f in [*files, _File(MANIFEST, Labelled(manifest, sensitivity))]:
            write_guarded(tmp / f.name, f.payload, plane, redaction=f.proof)
        if final.exists():
            old = final.parent / f".old-{secrets.token_hex(13)}"
            final.rename(old)
            try:
                tmp.rename(final)
            except BaseException:
                old.rename(final)
                raise
            shutil.rmtree(old)
        else:
            tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _reproof(html: bytes, sensitivity: Sensitivity) -> RedactionResult:
    """A stored HTML file re-proved: it must be a fixpoint of redact() (genuine redact() output
    always is), which also gives the guard a RedactionResult for exactly these bytes."""
    try:
        text = html.decode("utf-8")
    except UnicodeDecodeError:
        raise EvidenceCorrupted("a stored snapshot is not UTF-8") from None
    result = redact(Labelled(text, sensitivity), key=get_settings().redaction_key)
    if not result.verified or result.text != text:
        raise EvidenceCorrupted("a stored snapshot is not a verified redaction")
    return result


def write_last_accepted(redacted: RedactedEvidence, target: str, plane: Plane) -> Path:
    final = last_accepted_dir(target)
    files = [
        _html_file(PAGE_HTML, redacted.html, redacted.sensitivity, redacted.redaction),
        _File(PAGE_PNG, Labelled(redacted.png, redacted.sensitivity)),
    ]
    manifest = PageManifest(
        files={f.name: file_digest(f.payload.value) for f in files},
        label=redacted.sensitivity,
        page=redacted.side(),
        redaction=redacted.redaction_block(),
    )
    _write_dir(final, files, redacted.sensitivity, plane, manifest.dumps(), replace_existing=True)
    return final


def write_heal_bundle(heal_id: int, target: str, after: RedactedEvidence, plane: Plane) -> str:
    """Returns the evidence_uri. `before` is copied from last-accepted/, re-verified; if that
    is corrupted the bundle records why it has no before half."""
    final = heal_bundle_dir(heal_id)
    label = after.sensitivity
    files = [
        _html_file(AFTER_HTML, after.html, label, after.redaction),
        _File(AFTER_PNG, Labelled(after.png, label)),
    ]
    before: SideBlock | None = None
    before_reason: Literal["corrupted", "label_mismatch"] | None = None
    source = last_accepted_dir(target, create_key=False)
    if source.exists() or is_link(source):
        try:
            page = read_page_manifest(source)
            if page.label is not label:
                # Fix round 1: never re-attach a different label to stored bytes (a CLIENT
                # page reused as the before half of a SYNTHETIC bundle would launder it).
                before_reason = "label_mismatch"
            else:
                html = verified_bytes(source, page.files, PAGE_HTML)
                png = verified_bytes(source, page.files, PAGE_PNG)
                files += [
                    _html_file(BEFORE_HTML, html, label, _reproof(html, label)),
                    _File(BEFORE_PNG, Labelled(png, label)),
                ]
                before = page.page
        except EvidenceCorrupted:
            before_reason = "corrupted"
    manifest = HealManifest(
        files={f.name: file_digest(f.payload.value) for f in files},
        before=before,
        before_unavailable_reason=before_reason,
        after=after.side(),
        redaction=after.redaction_block(),
    )
    _write_dir(final, files, label, plane, manifest.dumps(), replace_existing=False)
    return str(final)


def promote_heal_bundle_to_last_accepted(heal_id: int, target: str, plane: Plane) -> Path:
    """A promoted run's accepted page is its bundle's 'after' half, re-verified first."""
    bundle = heal_bundle_dir(heal_id)
    manifest = read_heal_manifest(bundle)
    html = verified_bytes(bundle, manifest.files, AFTER_HTML)
    png = verified_bytes(bundle, manifest.files, AFTER_PNG)
    label = _stored_sensitivity(target)
    files = [
        _html_file(PAGE_HTML, html, label, _reproof(html, label)),
        _File(PAGE_PNG, Labelled(png, label)),
    ]
    page = PageManifest(
        files={f.name: file_digest(f.payload.value) for f in files},
        label=label,
        page=manifest.after,
        redaction=manifest.redaction,
    )
    final = last_accepted_dir(target)
    _write_dir(final, files, label, plane, page.dumps(), replace_existing=True)
    return final


def _scope_file_basis(target: str) -> str | None:
    """The basis the repo's scope YAML for `target` states, or None when there is no readable,
    valid file. A file the database cannot change: the mirror's counterpart for the decision
    below."""
    try:
        return load_scope(scope_file(f"{target}.yaml")).basis.value
    except ScopeRefusal:
        return None


def _sensitivity_for(mirror_basis: str | None, file_basis: str | None) -> Sensitivity:
    """SYNTHETIC only when both the mirror row and the scope file say `self-owned`; anything
    else -- a disagreement, a missing row, an unreadable file -- is CLIENT_CONFIDENTIAL. The
    worker writes the mirror every run, so on its own the mirror could be made to relabel a
    client's evidence (and drop its retention cutoff)."""
    both_self_owned = mirror_basis == Basis.SELF_OWNED and file_basis == Basis.SELF_OWNED
    return Sensitivity.SYNTHETIC if both_self_owned else Sensitivity.CLIENT_CONFIDENTIAL


def _stored_sensitivity(target: str) -> Sensitivity:
    """The label re-attached when stored files are re-written: derived from the scope record's
    basis as ScopeRecord.sensitivity derives it (R4), checked against the scope file as well as
    the database mirror, and fail-closed to CLIENT_CONFIDENTIAL."""
    with Session() as s:
        basis = s.scalars(
            select(ScopeRecordRow.basis).where(ScopeRecordRow.target == target).limit(1)
        ).first()
    return _sensitivity_for(basis, _scope_file_basis(target))


def store(evidence: Evidence, plane: Plane, *, target: str, heal_id: int | None = None) -> str:
    """capture -> scrub -> redact -> verify -> egress guard -> write. `heal_id=None` writes
    last-accepted/<target>/. Returns the directory written."""
    redacted = redact_evidence(evidence, key=get_settings().redaction_key)
    if heal_id is None:
        return str(write_last_accepted(redacted, target, plane))
    return write_heal_bundle(heal_id, target, redacted, plane)


__all__ = [
    "PII_MASK_BUDGET",
    "PiiMasker",
    "RedactedEvidence",
    "RedactionFailed",
    "UnsafeEvidencePath",
    "heal_bundle_dir",
    "last_accepted_dir",
    "pii_node_ids",
    "promote_heal_bundle_to_last_accepted",
    "redact_evidence",
    "scrub_credentials",
    "store",
    "strip_png_metadata",
    "write_heal_bundle",
    "write_last_accepted",
]
