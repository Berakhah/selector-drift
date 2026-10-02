"""Evidence bundle layout and manifest (API spec 7.2). Readers live here; writers are Task 33's.

Every file name in a bundle is one of the constants below -- never derived from page content
or a field name. A manifest that lists anything else is corrupted, and verified_bytes reads
only a constant name, so no manifest can steer a read outside its bundle directory.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from selector_drift.heallog.capture import BBox

LAST_ACCEPTED_DIR = "last-accepted"
HEALS_DIR = "heals"
MANIFEST = "manifest.json"
PAGE_HTML, PAGE_PNG = "page.html", "page.png"
BEFORE_HTML, BEFORE_PNG = "before.html", "before.png"
AFTER_HTML, AFTER_PNG = "after.html", "after.png"
BUNDLE_FILES = frozenset({PAGE_HTML, PAGE_PNG, BEFORE_HTML, BEFORE_PNG, AFTER_HTML, AFTER_PNG})

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
    before_unavailable_reason: Literal["corrupted"] | None = None
    after: SideBlock
    redaction: RedactionBlock

    @field_validator("files")
    @classmethod
    def _valid_files(cls, files: dict[str, str]) -> dict[str, str]:
        return _check_files(files)

    def dumps(self) -> bytes:
        return self.model_dump_json(exclude_none=True, indent=2).encode("utf-8")


class PageManifest(_Frozen):
    version: Literal[1] = 1
    files: dict[str, str]
    page: SideBlock
    redaction: RedactionBlock

    @field_validator("files")
    @classmethod
    def _valid_files(cls, files: dict[str, str]) -> dict[str, str]:
        return _check_files(files)

    def dumps(self) -> bytes:
        return self.model_dump_json(exclude_none=True, indent=2).encode("utf-8")


def file_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_heal_manifest(bundle: Path) -> HealManifest:
    try:
        return HealManifest.model_validate_json((bundle / MANIFEST).read_bytes())
    except (OSError, ValidationError) as exc:
        raise EvidenceCorrupted(f"heal bundle {bundle.name}: manifest unreadable") from exc


def read_page_manifest(directory: Path) -> PageManifest:
    try:
        return PageManifest.model_validate_json((directory / MANIFEST).read_bytes())
    except (OSError, ValidationError) as exc:
        raise EvidenceCorrupted(f"{directory.name}: manifest unreadable") from exc


def verified_bytes(directory: Path, files: Mapping[str, str], name: str) -> bytes:
    """The file's bytes, only if they hash to what the manifest recorded."""
    if name not in BUNDLE_FILES:
        raise EvidenceCorrupted(f"{name!r} is not a bundle file name")
    expected = files.get(name)
    if expected is None:
        raise EvidenceCorrupted(f"{name} is not listed in the manifest")
    try:
        data = (directory / name).read_bytes()
    except OSError as exc:
        raise EvidenceCorrupted(f"{name} is missing") from exc
    if file_digest(data) != expected:
        raise EvidenceCorrupted(f"{name} does not match its manifest hash")
    return data
