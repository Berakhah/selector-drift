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
3. bok-core redact() over the scrubbed DOM; an unverified result raises RedactionFailed.
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
import re
import secrets
import shutil
import struct
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NoReturn, SupportsIndex

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
from selector_drift.scope.record import Basis

_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")  # scope.record.TARGET_SLUG
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_KEEP = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS"})
# A separator no detector can match across ('☃' is in no detector's character class and
# survives normalisation), so one redact() call serves every element of a page.
_SEP = "\n☃\n"
_SURROGATE = re.compile(r"\[(?:EMAIL|IBAN|PHONE|DIGIT_RUN|CARD|TAX_ID|ROUTING|ACCOUNT):[a-p]{12}\]")
_UNRENDERED = frozenset({"script", "style", "noscript", "template", "head", "title", "meta"})
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


def _rendered_segments(root: HtmlElement) -> Iterator[tuple[HtmlElement, str]]:
    """(element, a piece of text it paints). Its own text with its children's tails joined as
    rendered -- "1234<span></span>5678" paints one run -- and each painted attribute."""
    for el in root.iter():
        if not isinstance(el, HtmlElement) or not isinstance(el.tag, str):
            continue
        if el.tag in _UNRENDERED or any(a.tag in _UNRENDERED for a in el.iterancestors()):
            continue
        own = (el.text or "") + "".join(child.tail or "" for child in el)
        if own.strip():
            yield el, own
        for attr in _RENDERED_ATTRS:
            value = el.get(attr)
            if value and value.strip():
                yield el, value


def _changed(texts: list[str], *, key: bytes, sensitivity: Sensitivity) -> list[bool]:
    """For each text, would redact() change it? One call for the page; per text if a text
    carries the separator or the split does not line up."""
    if not texts:
        return []
    if not any("☃" in t for t in texts):
        result = redact(Labelled(_SEP.join(texts), sensitivity), key=key)
        if not result.verified:
            raise RedactionFailed(f"redaction verification failed: {', '.join(result.failures)}")
        parts = result.text.split(_SEP)
        if len(parts) == len(texts):
            return [bool(_SURROGATE.search(p)) for p in parts]
    out: list[bool] = []
    for text in texts:
        result = redact(Labelled(text, sensitivity), key=key)
        if not result.verified:
            raise RedactionFailed(f"redaction verification failed: {', '.join(result.failures)}")
        out.append(bool(_SURROGATE.search(result.text)))
    return out


def _flagged_elements(
    root: HtmlElement, *, key: bytes, sensitivity: Sensitivity
) -> list[HtmlElement]:
    segments = list(_rendered_segments(root))
    hits = _changed([t for _, t in segments], key=key, sensitivity=sensitivity)
    seen: set[int] = set()
    flagged: list[HtmlElement] = []
    for (el, _), hit in zip(segments, hits, strict=True):
        if hit and id(el) not in seen:
            seen.add(id(el))
            flagged.append(el)
    return flagged


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
            redacted_identifiers=sum(self.redaction.surrogate_count.values()),
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


def redact_evidence(evidence: Evidence, *, key: bytes) -> RedactedEvidence:
    """Unconditional -- SYNTHETIC evidence is redacted too (H7). Writes nothing."""
    label = evidence.dom.sensitivity
    raw = evidence.dom.value
    if len(raw.encode("utf-8")) > MAX_HTML_BYTES:
        raise RedactionFailed("the DOM is over its size cap; refused, not truncated")
    _check_minted_values(evidence)
    scrubbed, n_scrubbed = scrub_credentials(raw, sensitivity=label)
    result = redact(Labelled(scrubbed, label), key=key)
    if not result.verified:
        raise RedactionFailed(f"redaction verification failed: {', '.join(result.failures)}")
    root = parse(scrubbed)
    required = _flagged_elements(root, key=key, sensitivity=label) + credential_controls(root)
    missing = _uncovered(root, required, evidence.masked_node_ids)
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
        html=result.text.encode("utf-8"),
        png=strip_png_metadata(evidence.screenshot.value),
        sensitivity=label,
        redaction=result,
        scrubbed_credentials=evidence.scrubbed + n_scrubbed,
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
    if not isinstance(target, str) or not _TARGET_RE.fullmatch(target):
        raise ValueError(f"not a target name: {target!r:.40}")
    return target


def last_accepted_dir(target: str) -> Path:
    """`last-accepted/t-<keyed digest of the target>`: opaque, so a directory listing or an
    error naming a path never names a client (bok-core pointer 6)."""
    slug = _target_slug(target)
    key = get_settings().redaction_key
    digest = hmac.new(key, b"sd-last-accepted\x00" + slug.encode(), hashlib.sha256).hexdigest()
    return _store_root() / LAST_ACCEPTED_DIR / f"t-{digest[:32]}"


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
    before_reason: str | None = None
    source = last_accepted_dir(target)
    if source.exists() or is_link(source):
        try:
            page = read_page_manifest(source)
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
        before_unavailable_reason="corrupted" if before_reason else None,
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
        page=manifest.after,
        redaction=manifest.redaction,
    )
    final = last_accepted_dir(target)
    _write_dir(final, files, label, plane, page.dumps(), replace_existing=True)
    return final


def _stored_sensitivity(target: str) -> Sensitivity:
    """The label re-attached when stored files are re-written: derived from the scope record's
    basis exactly as ScopeRecord.sensitivity derives it (R4), fail-closed to
    CLIENT_CONFIDENTIAL when there is no record."""
    with Session() as s:
        basis = s.scalars(
            select(ScopeRecordRow.basis).where(ScopeRecordRow.target == target).limit(1)
        ).first()
    return Sensitivity.SYNTHETIC if basis == Basis.SELF_OWNED else Sensitivity.CLIENT_CONFIDENTIAL


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
