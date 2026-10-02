"""Replay from stored evidence only. Never a live page (heallog-cannot-refetch).

Replay always runs the cascade with record=False -- no resolutions row, no rung 5 -- and
always with the evidence's own run key, so rung 1 binds the portal and spec version the heal
ran under (run_key=None would bind portal "" and spec_version None).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from lxml.html import HtmlElement

from selector_drift.heallog.bundle import (
    AFTER_HTML,
    EvidenceCorrupted,
    read_heal_manifest,
    verified_bytes,
)
from selector_drift.heallog.capture import Evidence, dom_sha256
from selector_drift.resolve.cache import CachedSelector, CacheReader
from selector_drift.resolve.cascade import parse_run_key, resolve_cascade
from selector_drift.resolve.escalation import EscalateToHuman
from selector_drift.resolve.page import NODE_ATTR, StaticPageSource
from selector_drift.resolve.trace import CascadeTrace, ResolvedElement
from selector_drift.spec.schema import FieldTarget

__all__ = [
    "CacheReader",
    "EvidenceCorrupted",
    "ExpectedHeal",
    "HealTimeCache",
    "NotReproducible",
    "replay",
    "replay_bundle",
]


# LivePageSource's node id shape (browser/page_source.py _LIVE_ID). heallog may not import
# browser/, so the shape is restated here; a live heal's ExpectedHeal.node_id is one of these.
_LIVE_TOKEN = re.compile(r"^n\d{1,7}$")


class NotReproducible(Exception):
    """Intact evidence replayed to a different outcome (B5: an anecdote, and published as one)."""


@dataclass(frozen=True)
class HealTimeCache:
    """Read-only: the single entry rung 1 held when the heal happened."""

    entry: CachedSelector | None

    def get(
        self, target: str, field: str, spec_version: int | None = None
    ) -> CachedSelector | None:
        return self.entry


@dataclass(frozen=True)
class ExpectedHeal:
    winning_rung: int
    node_id: str


def _checked_run_key(run_key: str) -> str:
    try:
        parse_run_key(run_key)
    except ValueError as exc:
        raise EvidenceCorrupted("the evidence's run key is not a run key") from exc
    return run_key


def _page(dom: str) -> StaticPageSource:
    try:
        return StaticPageSource(dom)
    except ValueError as exc:
        raise EvidenceCorrupted("the stored DOM is empty") from exc


def _expected_path(page: StaticPageSource, node_id: str) -> str:
    """R1. A live heal names its cell by data-sd-node token; replay names it by lxml path.
    Translate the token through the stored DOM -- exactly one element must carry it."""
    if not _LIVE_TOKEN.fullmatch(node_id):
        return node_id
    hits = page.root().xpath(f"//*[@{NODE_ATTR}=$n]", n=node_id)
    elements = [h for h in hits if isinstance(h, HtmlElement)] if isinstance(hits, list) else []
    if len(elements) != 1:
        raise EvidenceCorrupted(
            f"the heal's node id matches {len(elements)} elements in the stored DOM, not one"
        )
    return page.node_id(elements[0])


def _run(
    dom: str, run_key: str, target: FieldTarget, cache: CacheReader
) -> tuple[ResolvedElement, CascadeTrace]:
    page = _page(dom)
    try:
        return resolve_cascade(page, target, cache, record=False, run_key=run_key)
    except EscalateToHuman as exc:
        raise NotReproducible("no rung 1-4 resolved against the stored DOM") from exc


def replay(
    evidence: Evidence, target: FieldTarget, cache: CacheReader
) -> tuple[ResolvedElement, CascadeTrace]:
    if dom_sha256(evidence.dom.value) != evidence.sha256:
        raise EvidenceCorrupted("the stored DOM does not match its recorded hash")
    run_key = _checked_run_key(evidence.run_key)
    return _run(evidence.dom.value, run_key, target, cache)


def replay_bundle(
    bundle: Path, target: FieldTarget, cache: CacheReader, expected: ExpectedHeal
) -> tuple[ResolvedElement, CascadeTrace]:
    manifest = read_heal_manifest(bundle)
    try:
        dom = verified_bytes(bundle, manifest.files, AFTER_HTML).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceCorrupted(f"{AFTER_HTML} is not UTF-8") from exc
    run_key = _checked_run_key(manifest.after.run_key)
    expected_path = _expected_path(_page(dom), expected.node_id)
    element, trace = _run(dom, run_key, target, cache)
    if trace.winner != expected.winning_rung:
        raise NotReproducible(
            f"rung {trace.winner} won on replay; rung {expected.winning_rung} won live"
        )
    if element.node_id != expected_path:
        raise NotReproducible("replay resolved a different element")
    return element, trace
