from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bok_core.boundary.sensitivity import Sensitivity

from selector_drift.heallog.bundle import (
    AFTER_HTML,
    AFTER_PNG,
    MANIFEST,
    HealManifest,
    NodeBox,
    RedactionBlock,
    SideBlock,
    file_digest,
)
from selector_drift.heallog.capture import BLANK_PNG, VIEWPORT, Evidence
from selector_drift.heallog.replay import HealTimeCache
from selector_drift.resolve.cascade import resolve_cascade
from selector_drift.resolve.page import StaticPageSource
from selector_drift.spec.loader import load_spec

# Synthetic by construction: obviously invented names, no real person (bench spec 4).
BENCH_HTML = """<!doctype html><html><body>
<table id="results"><thead><tr>
<th>Claim Number</th><th>Claimant</th><th>Amount</th></tr></thead><tbody>
<tr><td data-testid="claim-number">AB12345678</td><td>Test Person One</td><td>100.00</td></tr>
<tr><td data-testid="claim-number">CD23456789</td><td>Test Person Two</td><td>250.50</td></tr>
<tr><td data-testid="claim-number">EF34567890</td><td>Test Person Three</td><td>999.99</td></tr>
</tbody></table></body></html>"""

CLAIM_NUMBER = load_spec(Path("specs/testbench.yaml")).targets[0]
RUN_KEY = "testbench:1:2026-09-07"


@dataclass(frozen=True)
class CapturedHeal:
    evidence: Evidence
    cache: HealTimeCache
    winning_rung: int
    node_id: str
    selector_after: str


@pytest.fixture
def captured_heal() -> CapturedHeal:
    html = BENCH_HTML.replace("Claim Number", "Claim Reference")  # a label reword
    ev = Evidence.from_static(html, run_key=RUN_KEY, sensitivity=Sensitivity.SYNTHETIC)
    cache = HealTimeCache(entry=None)  # cold: rung 1 declines, so any win is a heal
    el, trace = resolve_cascade(
        StaticPageSource(html), CLAIM_NUMBER, cache, record=False, run_key=RUN_KEY
    )
    assert trace.winner is not None and trace.winner in (2, 3, 4)
    return CapturedHeal(ev, cache, trace.winner, el.node_id, el.selector)


def write_test_bundle(
    directory: Path, after_html: str, nodes: dict[str, NodeBox] | None = None
) -> Path:
    """A heal bundle written the way Task 33 writes one, without Task 33's redaction."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / AFTER_HTML).write_bytes(after_html.encode())
    (directory / AFTER_PNG).write_bytes(BLANK_PNG)
    manifest = HealManifest(
        files={AFTER_HTML: file_digest(after_html.encode()), AFTER_PNG: file_digest(BLANK_PNG)},
        after=SideBlock(
            run_key=RUN_KEY,
            captured_at=datetime(2026, 9, 7, 4, 12, tzinfo=UTC),
            viewport=VIEWPORT,
            nodes=nodes or {},
        ),
        redaction=RedactionBlock(scrubbed_credentials=0, redacted_identifiers=0, verified=True),
    )
    (directory / MANIFEST).write_bytes(manifest.dumps())
    return directory


def rewrite_manifest(directory: Path, **changes: object) -> None:
    data = json.loads((directory / MANIFEST).read_text())
    data.update(changes)
    (directory / MANIFEST).write_text(json.dumps(data))
