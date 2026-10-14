"""Evidence bundle layout and manifest (API spec 7.2). Readers live here; writers are Task 33's.

Every file name in a bundle is one of the constants below -- never derived from page content
or a field name. A manifest that lists anything else is corrupted, and verified_bytes reads
only a constant name, so no manifest can steer a read outside its bundle directory.

Task 33 (S-M4, manifest exactness):
- A manifest must list exactly the files its kind requires: a heal bundle after.html and
  after.png, plus before.html and before.png together and only when its `before` block is
  present; a last-accepted page page.html and page.png and nothing else.
- read_*_manifest also checks the directory against the manifest: every regular file in it
  except the manifest must be listed, and every listed file must be there. An extra, unlisted
  file is corruption, not something to ignore.
- No read follows a link: the directory, the manifest and every file are refused if they are a
  symlink or a junction, or not a regular file.
- Every read is capped (MAX_MANIFEST_BYTES, MAX_HTML_BYTES, MAX_PNG_BYTES) by stat() before it
  starts and by the read itself, so a planted multi-gigabyte file is refused, never parsed.
- The digest is a plain sha256 (the spec 7.2 shape). It detects corruption, not tampering by
  someone who can write the CLIENT-plane store -- declared in docs/methodology.md.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal

from bok_core.boundary.sensitivity import Sensitivity
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from selector_drift.heallog.capture import BBox

LAST_ACCEPTED_DIR = "last-accepted"
HEALS_DIR = "heals"
MANIFEST = "manifest.json"
PAGE_HTML, PAGE_PNG = "page.html", "page.png"
BEFORE_HTML, BEFORE_PNG = "before.html", "before.png"
AFTER_HTML, AFTER_PNG = "after.html", "after.png"
BUNDLE_FILES = frozenset({PAGE_HTML, PAGE_PNG, BEFORE_HTML, BEFORE_PNG, AFTER_HTML, AFTER_PNG})
HEAL_REQUIRED = frozenset({AFTER_HTML, AFTER_PNG})
BEFORE_PAIR = frozenset({BEFORE_HTML, BEFORE_PNG})
PAGE_FILES = frozenset({PAGE_HTML, PAGE_PNG})

MIB = 1024 * 1024
MAX_MANIFEST_BYTES = 2 * MIB
MAX_HTML_BYTES = 20 * MIB
MAX_PNG_BYTES = 50 * MIB


def size_cap(name: str) -> int:
    if name == MANIFEST:
        return MAX_MANIFEST_BYTES
    return MAX_PNG_BYTES if name.endswith(".png") else MAX_HTML_BYTES


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvidenceCorrupted(Exception):
    """A stored file does not match its manifest. Deliberately NOT a NotReproducible:
    damaged evidence says nothing about the resolver (API spec 7.3)."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NodeBox(_Frozen):
    node_id: str
    bbox: BBox


class SideBlock(_Frozen):
    run_key: str
    captured_at: datetime
    viewport: tuple[int, int]
    nodes: dict[str, NodeBox]  # field name -> recorded box; a key, never a path


class RedactionBlock(_Frozen):
    scrubbed_credentials: int
    redacted_identifiers: int
    verified: bool


def _check_files(files: dict[str, str]) -> dict[str, str]:
    for name, digest in files.items():
        if name not in BUNDLE_FILES:
            raise ValueError(f"{name!r} is not a bundle file name")
        if not _DIGEST.fullmatch(digest):
            raise ValueError(f"digest for {name} is not sha256:<64 hex>")
    return files


class HealManifest(_Frozen):
    version: Literal[1] = 1
    files: dict[str, str]  # name -> "sha256:<hex>"
    before: SideBlock | None = None  # absent on a target's first heal
    # Found 2026-09-26, not in the spec's JSON: present only when last-accepted/ failed its
    # own hash at heal time, so the view can say "corrupted" rather than "not captured".
    # Fix round 1: "label_mismatch" -- last-accepted was stored under a different sensitivity
    # label than this capture, so it is not reused (no relabelling of client bytes).
    before_unavailable_reason: Literal["corrupted", "label_mismatch"] | None = None
    after: SideBlock
    redaction: RedactionBlock

    @field_validator("files")
    @classmethod
    def _valid_files(cls, files: dict[str, str]) -> dict[str, str]:
        return _check_files(files)

    @model_validator(mode="after")
    def _exact_files(self) -> HealManifest:
        names = set(self.files)
        expected = set(HEAL_REQUIRED) | (set(BEFORE_PAIR) if self.before is not None else set())
        if names != expected:
            raise ValueError("a heal manifest must list exactly its after (and before) files")
        if self.before is not None and self.before_unavailable_reason is not None:
            raise ValueError("a heal manifest cannot have a before half and a reason it has none")
        return self

    def dumps(self) -> bytes:
        return self.model_dump_json(exclude_none=True, indent=2).encode("utf-8")


class PageManifest(_Frozen):
    version: Literal[1] = 1
    files: dict[str, str]
    label: Sensitivity  # the capture's sensitivity label (fix round 1); reuse must match
    page: SideBlock
    redaction: RedactionBlock

    @field_validator("files")
    @classmethod
    def _valid_files(cls, files: dict[str, str]) -> dict[str, str]:
        files = _check_files(files)
        if set(files) != PAGE_FILES:
            raise ValueError("a page manifest must list exactly page.html and page.png")
        return files

    def dumps(self) -> bytes:
        return self.model_dump_json(exclude_none=True, indent=2).encode("utf-8")


def file_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def is_link(path: Path) -> bool:
    """A symlink, or a Windows junction (which is_symlink() does not report)."""
    return path.is_symlink() or path.is_junction()


def read_capped(path: Path, cap: int) -> bytes:
    """A regular file's bytes, never through a link and never more than `cap` of them.
    Raises OSError (FileNotFoundError when absent) or EvidenceCorrupted."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or path.is_junction():
        raise EvidenceCorrupted(f"{path.name} is a link")
    if not stat.S_ISREG(info.st_mode):
        raise EvidenceCorrupted(f"{path.name} is not a regular file")
    if info.st_size > cap:
        raise EvidenceCorrupted(f"{path.name} is over its {cap} byte cap")
    with open(path, "rb") as handle:
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise EvidenceCorrupted(f"{path.name} is over its {cap} byte cap")
    return data


def _check_directory(directory: Path, listed: Mapping[str, str]) -> None:
    """The directory holds exactly the manifest and the files it lists, and no link."""
    if is_link(directory):
        raise EvidenceCorrupted(f"{directory.name} is a link")
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise EvidenceCorrupted(f"{directory.name} cannot be listed") from exc
    on_disk: set[str] = set()
    for entry in entries:
        if is_link(entry) or not entry.is_file():
            raise EvidenceCorrupted(f"{directory.name} holds something that is not a file")
        on_disk.add(entry.name)
    missing = sorted(set(listed) - on_disk)
    if missing:
        raise EvidenceCorrupted(f"{missing[0]} is missing")
    if on_disk != set(listed) | {MANIFEST}:
        raise EvidenceCorrupted(
            f"{directory.name} does not hold exactly its manifest's files (an unlisted extra)"
        )


def read_heal_manifest(bundle: Path) -> HealManifest:
    try:
        manifest = HealManifest.model_validate_json(
            read_capped(bundle / MANIFEST, MAX_MANIFEST_BYTES)
        )
    except (OSError, ValidationError) as exc:
        raise EvidenceCorrupted(f"heal bundle {bundle.name}: manifest unreadable") from exc
    _check_directory(bundle, manifest.files)
    return manifest


def read_page_manifest(directory: Path) -> PageManifest:
    try:
        manifest = PageManifest.model_validate_json(
            read_capped(directory / MANIFEST, MAX_MANIFEST_BYTES)
        )
    except (OSError, ValidationError) as exc:
        raise EvidenceCorrupted(f"{directory.name}: manifest unreadable") from exc
    _check_directory(directory, manifest.files)
    return manifest


def verified_bytes(directory: Path, files: Mapping[str, str], name: str) -> bytes:
    """The file's bytes, only if they hash to what the manifest recorded."""
    if name not in BUNDLE_FILES:
        raise EvidenceCorrupted(f"{name!r} is not a bundle file name")
    expected = files.get(name)
    if expected is None:
        raise EvidenceCorrupted(f"{name} is not listed in the manifest")
    try:
        data = read_capped(directory / name, size_cap(name))
    except OSError as exc:
        raise EvidenceCorrupted(f"{name} is missing") from exc
    if file_digest(data) != expected:
        raise EvidenceCorrupted(f"{name} does not match its manifest hash")
    return data
