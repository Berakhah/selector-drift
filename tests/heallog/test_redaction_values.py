"""Task 33 fix round 1: redact VALUES, never markup (C1); identifiers split by markup or
entities (I1); the label recorded and checked on reuse; the redaction deadline; opaque,
key-rotation-stable last-accepted names; slug errors that name nothing."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import lxml.html
import pytest
from bok_core.boundary.egress import Plane
from bok_core.boundary.sensitivity import Sensitivity

from selector_drift.config import get_settings
from selector_drift.heallog import evidence as evidence_mod
from selector_drift.heallog.bundle import read_heal_manifest
from selector_drift.heallog.capture import Evidence, capture
from selector_drift.heallog.evidence import (
    PiiMasker,
    RedactionFailed,
    last_accepted_dir,
    redact_evidence,
    scrub_credentials,
    store,
    write_heal_bundle,
    write_last_accepted,
)
from selector_drift.heallog.replay import ExpectedHeal, HealTimeCache, replay_bundle
from tests.heallog.conftest import BENCH_HTML, CLAIM_NUMBER, CapturedHeal
from tests.heallog.test_redaction_order import CC, KEY, RUN_KEY, ClientPage

SYN = Sensitivity.SYNTHETIC

STATUS_ROW = (
    '<html><body><table><tr><th data-sd-node="h1">Status</th><th data-sd-node="h2">Adjuster'
    '</th><th data-sd-node="h3">Amount</th></tr><tr><td class="st" data-sd-node="n1">Open</td>'
    '<td data-sd-node="n2" title="adjuster@example.invalid">Desk 4</td>'
    '<td data-sd-node="n3">1,200.00</td></tr></table></body></html>'
)

# (name, cell markup, text that must not survive anywhere in the bundle)
SPLIT: list[tuple[str, str, list[bytes]]] = [
    (
        "card-in-spans",
        '<span data-sd-node="s1">4111</span> <span data-sd-node="s2">1111</span> '
        '<span data-sd-node="s3">1111</span> <span data-sd-node="s4">1111</span>',
        [b"4111"],
    ),
    ("account-across-bold", '1234<b data-sd-node="s1">5678</b>90', [b"1234", b"5678"]),
    (
        "ssn-in-spans",
        '<span data-sd-node="s1">123</span>-<span data-sd-node="s2">45</span>-'
        '<span data-sd-node="s3">6789</span>',
        [b">123<", b">6789<"],
    ),
    ("email-entity", "jane.okafor&#64;example.invalid", [b"okafor", b"&#64;example"]),
    ("email-wbr", "jane.okafor<wbr>@example.invalid", [b"okafor", b"example.invalid"]),
    (
        "email-split-span",
        '<span data-sd-node="s1">jane.okafor</span>@example.invalid',
        [b"okafor", b"example.invalid"],
    ),
]
ATTRIBUTES = (
    '<a data-sd-node="a1" href="mailto:jane.okafor@example.invalid">mail</a>'
    '<span data-sd-node="a2" data-email="jane.okafor@example.invalid">x</span>'
    '<span data-sd-node="a3" aria-label="Contact jane.okafor@example.invalid">y</span>'
)


@pytest.fixture(autouse=True)
def evidence_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    root = tmp_path / "evidence"
    monkeypatch.setenv("SD_EVIDENCE_ROOT", str(root))
    monkeypatch.setenv("SD_REDACTION_KEY", KEY.hex())
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def _capture(html: str, label: Sensitivity) -> Evidence:
    return asyncio.run(
        capture(ClientPage(html, label), run_key=RUN_KEY, masker=PiiMasker(KEY, label))
    )


def _skeleton(html: str) -> list[tuple[str, str | None]]:
    root = lxml.html.document_fromstring(html)
    return [(str(el.tag), el.get("data-sd-node")) for el in root.iter() if isinstance(el.tag, str)]


def _stored(uri: str) -> str:
    return (Path(uri) / "after.html").read_text(encoding="utf-8")


def _blob(uri: str) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(Path(uri).rglob("*")) if p.is_file())


def _pages() -> list[tuple[str, str]]:
    cells = "".join(
        f'<tr><td data-sd-node="c{i}">{body}</td></tr>' for i, (_, body, _) in enumerate(SPLIT)
    )
    return [
        ("status-row", STATUS_ROW),
        ("split-and-attributes", f"<html><body><table>{cells}</table>{ATTRIBUTES}</body></html>"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize("label", [CC, SYN], ids=str)
@pytest.mark.parametrize("name,html", _pages(), ids=[n for n, _ in _pages()])
def test_redaction_keeps_the_element_skeleton_and_every_node_id(
    name: str, html: str, label: Sensitivity
) -> None:
    """C1: an attribute email used to be matched ACROSS markup by the quoted-local-part form
    and the row, its cells and their node ids were replaced by one surrogate."""
    ev = _capture(html, label)
    scrubbed, _ = scrub_credentials(ev.dom.value, sensitivity=label)
    uri = store(ev, Plane.CLIENT, target="client-portal", heal_id=1)
    assert _skeleton(_stored(uri)) == _skeleton(scrubbed), name
    blob = _blob(uri)
    assert b"jane.okafor@" not in blob and b"adjuster@example" not in blob


@pytest.mark.unit
def test_the_status_row_keeps_its_cells_and_text() -> None:
    uri = store(_capture(STATUS_ROW, CC), Plane.CLIENT, target="client-portal", heal_id=1)
    cells = lxml.html.document_fromstring(_stored(uri)).xpath("//tr[2]/td")
    assert [(c.get("data-sd-node"), c.text_content()) for c in cells] == [
        ("n1", "Open"),
        ("n2", "Desk 4"),
        ("n3", "1,200.00"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize("name,body,needles", SPLIT, ids=[n for n, _, _ in SPLIT])
def test_an_identifier_split_by_markup_or_entities_is_redacted_and_masked(
    name: str, body: str, needles: list[bytes]
) -> None:
    """I1: detection runs over each block's rendered text, entities decoded."""
    html = f'<html><body><table><tr><td data-sd-node="n1">{body}</td></tr></table></body></html>'
    ev = _capture(html, CC)
    assert "n1" in ev.masked_node_ids, name
    uri = store(ev, Plane.CLIENT, target="client-portal", heal_id=1)
    blob = _blob(uri)
    assert not [n for n in needles if n in blob], name
    assert read_heal_manifest(Path(uri)).redaction.redacted_identifiers >= 1


@pytest.mark.unit
def test_a_split_identifier_left_unmasked_is_refused() -> None:
    html = (
        '<html><body><p data-sd-node="p1"><span data-sd-node="s1">4111</span> '
        '<span data-sd-node="s2">1111</span> <span data-sd-node="s3">1111</span> '
        '<span data-sd-node="s4">1111</span></p></body></html>'
    )
    unmasked = Evidence.from_static(html, run_key=RUN_KEY, sensitivity=SYN)
    with pytest.raises(RedactionFailed, match="not masked"):
        redact_evidence(unmasked, key=KEY)


@pytest.mark.unit
def test_a_heal_replays_from_a_bundle_with_attribute_emails(captured_heal: CapturedHeal) -> None:
    """C1: replay runs on the stored page; it must be the page the heal saw."""
    root = lxml.html.document_fromstring(BENCH_HTML.replace("Claim Number", "Claim Reference"))
    for i, el in enumerate(e for e in root.iter() if isinstance(e.tag, str)):
        el.set("data-sd-node", f"n{i}")
    for td in root.iter("td"):
        td.set("title", "adjuster@example.invalid")
    html = str(lxml.html.tostring(root, encoding="unicode", doctype="<!doctype html>"))
    ev = Evidence.from_static(
        html, run_key="testbench:1:2026-09-07", sensitivity=SYN, masker=PiiMasker(KEY, SYN)
    )
    uri = write_heal_bundle(1, "testbench", redact_evidence(ev, key=KEY), Plane.CLIENT)
    assert b"adjuster@example" not in _blob(uri)
    replay_bundle(
        Path(uri),
        CLAIM_NUMBER,
        HealTimeCache(entry=None),
        ExpectedHeal(captured_heal.winning_rung, captured_heal.node_id),
    )


@pytest.mark.unit
def test_a_before_half_stored_under_another_label_is_not_reused() -> None:
    """Folded minor: no relabelling of client bytes as SYNTHETIC."""
    write_last_accepted(
        redact_evidence(_capture(STATUS_ROW, CC), key=KEY), "testbench", Plane.CLIENT
    )
    after = redact_evidence(_capture(STATUS_ROW, SYN), key=KEY)
    manifest = read_heal_manifest(Path(write_heal_bundle(2, "testbench", after, Plane.CLIENT)))
    assert manifest.before is None
    assert manifest.before_unavailable_reason == "label_mismatch"


@pytest.mark.unit
def test_redaction_past_its_deadline_stores_nothing(
    evidence_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Folded minor (bok-core pointer 2): past the deadline it refuses, never truncates."""
    ev = _capture(STATUS_ROW, CC)
    with pytest.raises(RedactionFailed, match="deadline"):
        redact_evidence(ev, key=KEY, deadline_s=0.0)
    # The configured deadline (Settings) against a redactor slowed to past it.
    monkeypatch.setenv("SD_REDACTION_DEADLINE_S", "0.05")
    get_settings.cache_clear()
    real = evidence_mod.redact

    def slow(payload, *, key):  # type: ignore[no-untyped-def]
        time.sleep(0.06)
        return real(payload, key=key)

    monkeypatch.setattr(evidence_mod, "redact", slow)
    with pytest.raises(RedactionFailed, match="deadline"):
        store(ev, Plane.CLIENT, target="client-portal", heal_id=1)
    assert not list(evidence_root.rglob("*"))


@pytest.mark.unit
def test_last_accepted_names_survive_redaction_key_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_last_accepted(
        redact_evidence(_capture(STATUS_ROW, SYN), key=KEY), "testbench", Plane.CLIENT
    )
    before = last_accepted_dir("testbench")
    monkeypatch.setenv("SD_REDACTION_KEY", (b"another-test-only-key-not-a-secret!!").hex())
    get_settings.cache_clear()
    assert last_accepted_dir("testbench") == before and before.is_dir()
    assert "testbench" not in before.name


@pytest.mark.unit
def test_a_bad_target_name_is_not_echoed() -> None:
    with pytest.raises(ValueError) as refused:
        last_accepted_dir("Acme Insurance Claims")
    assert "Acme" not in str(refused.value)


@pytest.mark.unit
def test_the_redacted_html_is_handed_to_the_guard_with_a_proof_for_its_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []
    real = evidence_mod.write_guarded

    def spy(path, payload, plane, *, redaction=None):  # type: ignore[no-untyped-def]
        if path.name == "after.html":
            seen.append(redaction is not None and redaction.text.encode() == payload.value)
        real(path, payload, plane, redaction=redaction)

    monkeypatch.setattr(evidence_mod, "write_guarded", spy)
    store(_capture(STATUS_ROW, CC), Plane.CLIENT, target="client-portal", heal_id=1)
    assert seen == [True]
