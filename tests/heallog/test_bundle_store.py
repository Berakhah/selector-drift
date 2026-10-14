from __future__ import annotations

import copy
import dataclasses
import json
import pickle
import re
import struct
import zlib
from pathlib import Path

import lxml.html
import pytest
from bok_core.boundary.egress import Plane
from bok_core.boundary.sensitivity import Sensitivity

from selector_drift.config import get_settings
from selector_drift.heallog import evidence as evidence_mod
from selector_drift.heallog.bundle import (
    AFTER_PNG,
    MANIFEST,
    PAGE_HTML,
    EvidenceCorrupted,
    read_heal_manifest,
)
from selector_drift.heallog.capture import BLANK_PNG, BBox, Evidence
from selector_drift.heallog.evidence import (
    PiiMasker,
    RedactedEvidence,
    RedactionFailed,
    UnsafeEvidencePath,
    heal_bundle_dir,
    last_accepted_dir,
    pii_node_ids,
    promote_heal_bundle_to_last_accepted,
    redact_evidence,
    strip_png_metadata,
    write_heal_bundle,
    write_last_accepted,
)
from selector_drift.heallog.replay import ExpectedHeal, HealTimeCache, replay_bundle
from tests.heallog.conftest import BENCH_HTML, CLAIM_NUMBER, CapturedHeal

KEY = b"test-only-redaction-key-not-a-secret!"
PAGE = """<html><body><table><tr><th data-sd-node="n0">Claim Number</th></tr>
<tr><td data-sd-node="n1">AB12345678</td></tr></table></body></html>"""


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SD_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    monkeypatch.setenv("SD_REDACTION_KEY", KEY.hex())
    get_settings.cache_clear()
    yield tmp_path / "evidence"
    get_settings.cache_clear()


def _red(
    html: str = PAGE, run_key: str = "testbench:1:2026-09-07", png: bytes = BLANK_PNG
) -> RedactedEvidence:
    ev = Evidence.from_static(
        html,
        run_key=run_key,
        sensitivity=Sensitivity.SYNTHETIC,
        masker=PiiMasker(KEY, Sensitivity.SYNTHETIC),
        screenshot=png,
    )
    ev = dataclasses.replace(
        ev,
        node_ids={"claim_number": "n1"},
        boxes={"claim_number": BBox(x=8, y=30, width=120, height=22)},
    )
    return redact_evidence(ev, key=KEY)


def _stamped(html: str) -> str:
    root = lxml.html.document_fromstring(html)
    for i, el in enumerate(e for e in root.iter() if isinstance(e.tag, str)):
        el.set("data-sd-node", f"n{i}")
    return str(lxml.html.tostring(root, encoding="unicode", doctype="<!doctype html>"))


def _png_with_text(text: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    body = BLANK_PNG[8:]
    return BLANK_PNG[:8] + body[:25] + chunk(b"tEXt", b"Comment\x00" + text) + body[25:]


@pytest.mark.unit
def test_heal_bundle_manifest_is_exactly_the_spec_shape(root: Path) -> None:
    """API spec 7.2 -- field for field."""
    write_last_accepted(_red(run_key="testbench:1:2026-09-06"), "testbench", Plane.CLIENT)
    uri = write_heal_bundle(3, "testbench", _red(), Plane.CLIENT)
    data = json.loads((Path(uri) / MANIFEST).read_text())
    assert set(data) == {"version", "files", "before", "after", "redaction"}
    assert data["version"] == 1
    assert set(data["files"]) == {"before.html", "before.png", "after.html", "after.png"}
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", v) for v in data["files"].values())
    for side in ("before", "after"):
        assert set(data[side]) == {"run_key", "captured_at", "viewport", "nodes"}
        assert data[side]["viewport"] == [1280, 900]
        assert data[side]["captured_at"].endswith("Z")
    assert data["after"]["nodes"]["claim_number"] == {
        "node_id": "n1",
        "bbox": {"x": 8.0, "y": 30.0, "width": 120.0, "height": 22.0},
    }
    assert set(data["redaction"]) == {"scrubbed_credentials", "redacted_identifiers", "verified"}
    assert data["redaction"]["verified"] is True


@pytest.mark.unit
def test_a_failed_bundle_write_leaves_no_bundle_and_no_tmp_dir(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = evidence_mod.write_guarded
    calls = {"n": 0}

    def flaky(path, payload, plane, *, redaction=None):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real(path, payload, plane, redaction=redaction)

    monkeypatch.setattr(evidence_mod, "write_guarded", flaky)
    with pytest.raises(OSError):
        write_heal_bundle(4, "testbench", _red(), Plane.CLIENT)
    assert not heal_bundle_dir(4).exists()
    assert not list(root.rglob(".tmp-*"))


@pytest.mark.unit
def test_every_file_goes_through_the_egress_guard(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = evidence_mod.write_guarded
    written: list[str] = []
    monkeypatch.setattr(
        evidence_mod,
        "write_guarded",
        lambda path, payload, plane, *, redaction=None: (
            written.append(path.name),
            real(path, payload, plane, redaction=redaction),
        ),
    )
    uri = write_heal_bundle(5, "testbench", _red(), Plane.CLIENT)
    assert sorted(written) == sorted(p.name for p in Path(uri).iterdir())


@pytest.mark.unit
def test_a_heal_bundle_is_written_once(root: Path) -> None:
    write_heal_bundle(6, "testbench", _red(), Plane.CLIENT)
    with pytest.raises(FileExistsError):
        write_heal_bundle(6, "testbench", _red(), Plane.CLIENT)


@pytest.mark.unit
def test_last_accepted_is_replaced_whole(root: Path) -> None:
    write_last_accepted(_red(PAGE), "testbench", Plane.CLIENT)
    newer = PAGE.replace("Claim Number", "Claim Reference")
    write_last_accepted(_red(newer), "testbench", Plane.CLIENT)
    assert b"Claim Reference" in (last_accepted_dir("testbench") / PAGE_HTML).read_bytes()
    # Adaptation (bok-core pointer 6): the directory is an opaque keyed name, not the target.
    siblings = sorted(p.name for p in last_accepted_dir("testbench").parent.iterdir())
    assert siblings == [last_accepted_dir("testbench").name]
    assert "testbench" not in str(last_accepted_dir("testbench").relative_to(root))


@pytest.mark.unit
def test_screenshot_metadata_is_stripped(root: Path) -> None:
    shot = _png_with_text(b"Cookie: session=sid-7f3e9a1c")
    assert b"sid-7f3e9a1c" in shot
    uri = write_heal_bundle(7, "testbench", _red(png=shot), Plane.CLIENT)
    assert b"sid-7f3e9a1c" not in (Path(uri) / AFTER_PNG).read_bytes()
    assert strip_png_metadata(BLANK_PNG) == BLANK_PNG


@pytest.mark.unit
def test_a_screenshot_that_did_not_mask_an_identifier_node_is_refused() -> None:
    page = PAGE.replace("</table>", '</table><p data-sd-node="n9">jane.okafor@example.invalid</p>')
    unmasked = Evidence.from_static(
        page, run_key="testbench:1:2026-09-07", sensitivity=Sensitivity.SYNTHETIC
    )
    with pytest.raises(RedactionFailed, match="not masked"):
        redact_evidence(unmasked, key=KEY)


@pytest.mark.unit
def test_a_corrupted_last_accepted_is_recorded_not_silently_dropped(root: Path) -> None:
    write_last_accepted(_red(run_key="testbench:1:2026-09-06"), "testbench", Plane.CLIENT)
    (last_accepted_dir("testbench") / PAGE_HTML).write_text("<html>edited</html>")
    manifest = read_heal_manifest(Path(write_heal_bundle(8, "testbench", _red(), Plane.CLIENT)))
    assert manifest.before is None and manifest.before_unavailable_reason == "corrupted"


@pytest.mark.unit
def test_promotion_copies_the_bundles_after_half_into_last_accepted(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The label lookup reads Postgres; Task 26A's integration test covers it. Here: DB-free.
    monkeypatch.setattr(evidence_mod, "_stored_sensitivity", lambda t: Sensitivity.SYNTHETIC)
    uri = write_heal_bundle(9, "testbench", _red(), Plane.CLIENT)
    promote_heal_bundle_to_last_accepted(9, "testbench", Plane.CLIENT)
    assert (last_accepted_dir("testbench") / PAGE_HTML).read_bytes() == (
        Path(uri) / "after.html"
    ).read_bytes()
    (Path(uri) / "after.html").write_text("<html>edited</html>")
    with pytest.raises(EvidenceCorrupted):
        promote_heal_bundle_to_last_accepted(9, "testbench", Plane.CLIENT)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["../etc", "Testbench", "a/b", ""])
def test_a_target_name_cannot_escape_the_evidence_root(bad: str) -> None:
    with pytest.raises(ValueError):
        last_accepted_dir(bad)


# -- Task 33 additions -------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, -1, True, "7", 7.0])
def test_a_heal_bundle_path_comes_only_from_a_positive_integer(bad: object) -> None:
    with pytest.raises(ValueError):
        heal_bundle_dir(bad)  # type: ignore[arg-type]


@pytest.mark.unit
def test_promotion_of_a_client_bundle_re_proves_the_redaction(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At CLIENT_CONFIDENTIAL the HTML goes to the guard as REDACTED with a RedactionResult for
    exactly its bytes -- re-derived from the stored file, never a hand-built verified=True."""
    monkeypatch.setattr(
        evidence_mod, "_stored_sensitivity", lambda t: Sensitivity.CLIENT_CONFIDENTIAL
    )
    write_heal_bundle(10, "client-portal", _red(), Plane.CLIENT)
    seen: list[tuple[str, Sensitivity, bool]] = []
    real = evidence_mod.write_guarded

    def spy(path, payload, plane, *, redaction=None):  # type: ignore[no-untyped-def]
        seen.append((path.name, payload.sensitivity, redaction is not None))
        real(path, payload, plane, redaction=redaction)

    monkeypatch.setattr(evidence_mod, "write_guarded", spy)
    promote_heal_bundle_to_last_accepted(10, "client-portal", Plane.CLIENT)
    assert ("page.html", Sensitivity.REDACTED, True) in seen
    assert ("page.png", Sensitivity.CLIENT_CONFIDENTIAL, False) in seen
    from bok_core.boundary.sensitivity import SensitivityViolation

    with pytest.raises(SensitivityViolation):
        promote_heal_bundle_to_last_accepted(10, "client-portal", Plane.DEMO)


@pytest.mark.unit
def test_a_linked_store_directory_is_refused(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir(parents=True)
    try:
        (root / "heals").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create symlinks")
    with pytest.raises(UnsafeEvidencePath):
        write_heal_bundle(11, "testbench", _red(), Plane.CLIENT)
    assert not list(outside.iterdir())


@pytest.mark.unit
def test_evidence_is_never_pickled_or_copied() -> None:
    """S-M2: a pickled or copied Evidence is how it would reach a Prefect result store."""
    ev = Evidence.from_static(
        PAGE,
        run_key="testbench:1:2026-09-07",
        sensitivity=Sensitivity.SYNTHETIC,
        masker=PiiMasker(KEY, Sensitivity.SYNTHETIC),
    )
    red = redact_evidence(ev, key=KEY)
    for obj in (ev, red):
        with pytest.raises(TypeError):
            pickle.dumps(obj)
        with pytest.raises(TypeError):
            copy.deepcopy(obj)
    with pytest.raises(TypeError):
        RedactedEvidence(
            object(),
            **{f.name: getattr(red, f.name) for f in dataclasses.fields(red) if f.name != "_token"},
        )
    assert "AB" not in repr(red)


@pytest.mark.unit
def test_the_pii_mask_collapses_to_containers_past_its_budget() -> None:
    """S-M5: 300 identifier cells would blow MAX_MASKED_NODES; the rows, then the table, are
    masked instead -- and the verification accepts an ancestor's mask."""
    rows = "".join(
        f'<tr data-sd-node="r{i}"><td data-sd-node="c{i}">user{i}@example.invalid</td></tr>'
        for i in range(300)
    )
    html = (
        '<html><body><table data-sd-node="t"><tbody data-sd-node="b">'
        f"{rows}</tbody></table></body></html>"
    )
    full = pii_node_ids(html, key=KEY, sensitivity=Sensitivity.SYNTHETIC, budget=1_000)
    assert len(full) == 300
    tight = pii_node_ids(html, key=KEY, sensitivity=Sensitivity.SYNTHETIC, budget=10)
    assert tight == frozenset({"b"})
    ev = Evidence.from_static(
        html,
        run_key="testbench:1:2026-09-07",
        sensitivity=Sensitivity.SYNTHETIC,
        masker=lambda d: tight,
    )
    assert redact_evidence(ev, key=KEY).redaction.verified


@pytest.mark.unit
def test_an_identifier_split_by_markup_is_still_masked() -> None:
    html = '<html><body><p data-sd-node="p">4111 1111<span></span> 1111 1111</p></body></html>'
    assert pii_node_ids(html, key=KEY, sensitivity=Sensitivity.SYNTHETIC) == frozenset({"p"})


@pytest.mark.unit
def test_a_heal_replays_from_its_redacted_bundle(root: Path, captured_heal: CapturedHeal) -> None:
    """Bok-core pointer 4: redaction surrogates every 8+ digit run (the claim numbers), and the
    heal must still replay on the REDACTED snapshot it is stored as."""
    # Stamped like a live snapshot (every element carries a node id) so the masker can name
    # the identifier cells; the stamp changes no lxml path.
    html = _stamped(BENCH_HTML.replace("Claim Number", "Claim Reference"))
    ev = Evidence.from_static(
        html,
        run_key="testbench:1:2026-09-07",
        sensitivity=Sensitivity.SYNTHETIC,
        masker=PiiMasker(KEY, Sensitivity.SYNTHETIC),
    )
    uri = write_heal_bundle(12, "testbench", redact_evidence(ev, key=KEY), Plane.CLIENT)
    stored = (Path(uri) / "after.html").read_text(encoding="utf-8")
    assert "AB12345678" not in stored
    replay_bundle(
        Path(uri),
        CLAIM_NUMBER,
        HealTimeCache(entry=None),
        ExpectedHeal(captured_heal.winning_rung, captured_heal.node_id),
    )
