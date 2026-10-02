import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from bok_core.boundary.sensitivity import Labelled, Sensitivity

from selector_drift.db.models import Resolution
from selector_drift.db.session import Session
from selector_drift.heallog.bundle import (
    AFTER_HTML,
    AFTER_PNG,
    MANIFEST,
    EvidenceCorrupted,
    file_digest,
    read_heal_manifest,
    verified_bytes,
)
from selector_drift.heallog.capture import Evidence
from selector_drift.heallog.replay import (
    ExpectedHeal,
    HealTimeCache,
    NotReproducible,
    replay,
    replay_bundle,
)
from selector_drift.resolve import cascade as cascade_mod
from selector_drift.resolve.cache import CachedSelector
from tests.heallog.conftest import (
    CLAIM_NUMBER,
    RUN_KEY,
    CapturedHeal,
    rewrite_manifest,
    write_test_bundle,
)


@pytest.mark.unit
def test_replay_reproduces_the_same_rung_and_element(captured_heal: CapturedHeal) -> None:
    el, trace = replay(captured_heal.evidence, CLAIM_NUMBER, captured_heal.cache)
    assert trace.winner == captured_heal.winning_rung
    assert el.selector == captured_heal.selector_after
    assert el.node_id == captured_heal.node_id
    assert trace.resolution_id is None


@pytest.mark.unit
def test_replay_always_hands_the_cascade_the_evidence_run_key(
    captured_heal: CapturedHeal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_key=None would bind rung 1 to portal "" and spec_version None (Task 17 ledger)."""
    seen: list[tuple[object, object]] = []
    real = cascade_mod.resolve_cascade

    def spy(*args: object, **kwargs: object) -> object:
        seen.append((kwargs.get("run_key"), kwargs.get("record")))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("selector_drift.heallog.replay.resolve_cascade", spy)
    replay(captured_heal.evidence, CLAIM_NUMBER, captured_heal.cache)
    assert seen == [(RUN_KEY, False)]


@pytest.mark.unit
def test_replay_with_the_heal_time_cache_entry_reproduces_a_rung_1_hit(
    captured_heal: CapturedHeal,
) -> None:
    """The run after a confirmed heal: rung 1 holds the healed selector and its anchor, and
    replaying the same stored DOM re-verifies it there, without reaching rungs 2-4."""
    el, _ = replay(captured_heal.evidence, CLAIM_NUMBER, captured_heal.cache)
    warm = HealTimeCache(
        entry=CachedSelector(selector=el.selector, anchor=el.anchor, set_by_rung=el.rung_id)
    )
    hit, trace = replay(captured_heal.evidence, CLAIM_NUMBER, warm)
    assert trace.winner == 1 and [a.rung for a in trace.attempts] == [1]
    assert hit.node_id == captured_heal.node_id


@pytest.mark.unit
def test_evidence_with_a_malformed_run_key_is_corrupted(captured_heal: CapturedHeal) -> None:
    broken = replace(captured_heal.evidence, run_key="not-a-run-key")
    with pytest.raises(EvidenceCorrupted, match="run key"):
        replay(broken, CLAIM_NUMBER, captured_heal.cache)


@pytest.mark.unit
def test_a_page_no_rung_can_resolve_is_not_reproducible(captured_heal: CapturedHeal) -> None:
    blank = Evidence.from_static(
        "<html><body><p>nothing here</p></body></html>",
        run_key=RUN_KEY,
        sensitivity=Sensitivity.SYNTHETIC,
    )
    with pytest.raises(NotReproducible):
        replay(blank, CLAIM_NUMBER, captured_heal.cache)


@pytest.mark.unit
def test_a_dom_that_fails_its_hash_is_corrupted_not_non_reproducible(
    captured_heal: CapturedHeal,
) -> None:
    """API spec 7.3. Damaged evidence says nothing about the resolver."""
    tampered = replace(
        captured_heal.evidence,
        dom=Labelled(value="<html></html>", sensitivity=Sensitivity.SYNTHETIC),
    )
    with pytest.raises(EvidenceCorrupted):
        replay(tampered, CLAIM_NUMBER, captured_heal.cache)
    assert not issubclass(EvidenceCorrupted, NotReproducible)


@pytest.mark.unit
def test_replay_bundle_reads_after_html_and_reproduces(
    captured_heal: CapturedHeal, tmp_path: Path
) -> None:
    bundle = write_test_bundle(tmp_path / "heals" / "7", captured_heal.evidence.dom.value)
    el, trace = replay_bundle(
        bundle,
        CLAIM_NUMBER,
        captured_heal.cache,
        ExpectedHeal(captured_heal.winning_rung, captured_heal.node_id),
    )
    assert el.node_id == captured_heal.node_id and trace.winner == captured_heal.winning_rung


@pytest.mark.unit
def test_a_tampered_after_html_is_corrupted(captured_heal: CapturedHeal, tmp_path: Path) -> None:
    bundle = write_test_bundle(tmp_path / "heals" / "7", captured_heal.evidence.dom.value)
    (bundle / AFTER_HTML).write_text(captured_heal.evidence.dom.value + "<!-- edited -->")
    with pytest.raises(EvidenceCorrupted, match="manifest"):
        replay_bundle(
            bundle,
            CLAIM_NUMBER,
            captured_heal.cache,
            ExpectedHeal(captured_heal.winning_rung, captured_heal.node_id),
        )


@pytest.mark.unit
def test_a_truncated_after_html_is_corrupted(captured_heal: CapturedHeal, tmp_path: Path) -> None:
    bundle = write_test_bundle(tmp_path / "heals" / "7", captured_heal.evidence.dom.value)
    whole = (bundle / AFTER_HTML).read_bytes()
    (bundle / AFTER_HTML).write_bytes(whole[: len(whole) // 2])
    with pytest.raises(EvidenceCorrupted):
        replay_bundle(
            bundle,
            CLAIM_NUMBER,
            captured_heal.cache,
            ExpectedHeal(captured_heal.winning_rung, captured_heal.node_id),
        )


@pytest.mark.unit
def test_a_missing_after_html_is_corrupted(captured_heal: CapturedHeal, tmp_path: Path) -> None:
    bundle = write_test_bundle(tmp_path / "heals" / "7", captured_heal.evidence.dom.value)
    (bundle / AFTER_HTML).unlink()
    with pytest.raises(EvidenceCorrupted, match="missing"):
        replay_bundle(bundle, CLAIM_NUMBER, captured_heal.cache, ExpectedHeal(2, "x"))


@pytest.mark.unit
def test_a_bundle_with_no_manifest_is_corrupted(
    tmp_path: Path, captured_heal: CapturedHeal
) -> None:
    (tmp_path / "heals" / "9").mkdir(parents=True)
    with pytest.raises(EvidenceCorrupted):
        replay_bundle(
            tmp_path / "heals" / "9",
            CLAIM_NUMBER,
            captured_heal.cache,
            ExpectedHeal(2, "anything"),
        )


@pytest.mark.unit
def test_a_manifest_with_a_malformed_run_key_is_corrupted(
    captured_heal: CapturedHeal, tmp_path: Path
) -> None:
    bundle = write_test_bundle(tmp_path / "heals" / "7", captured_heal.evidence.dom.value)
    after = read_heal_manifest(bundle).after.model_dump(mode="json")
    rewrite_manifest(bundle, after={**after, "run_key": "../../etc"})
    with pytest.raises(EvidenceCorrupted, match="run key"):
        replay_bundle(bundle, CLAIM_NUMBER, captured_heal.cache, ExpectedHeal(2, "x"))


@pytest.mark.unit
def test_a_manifest_naming_a_file_outside_the_bundle_is_corrupted(
    captured_heal: CapturedHeal, tmp_path: Path
) -> None:
    """File names are constants. A manifest that lists a path escape -- even one whose digest
    is right for the file it points at -- is refused at read time, and verified_bytes never
    reads a name that is not one of the bundle's constants."""
    outside = tmp_path / "heals" / "outside.html"
    bundle = write_test_bundle(tmp_path / "heals" / "7", captured_heal.evidence.dom.value)
    outside.write_bytes(b"<html>planted</html>")
    files = read_heal_manifest(bundle).files
    rewrite_manifest(bundle, files={**files, "../outside.html": file_digest(outside.read_bytes())})
    with pytest.raises(EvidenceCorrupted):
        read_heal_manifest(bundle)
    hostile = {"../outside.html": file_digest(outside.read_bytes())}
    with pytest.raises(EvidenceCorrupted):
        verified_bytes(bundle, hostile, "../outside.html")


@pytest.mark.unit
def test_a_hostile_field_name_is_only_ever_a_key(
    captured_heal: CapturedHeal, tmp_path: Path
) -> None:
    """A field name with path separators lives in the manifest's nodes map; nothing on disk is
    named after it, and the bundle still replays."""
    from selector_drift.heallog.bundle import NodeBox
    from selector_drift.heallog.capture import BBox

    hostile = "../../../evil\\..\\name"
    bundle = write_test_bundle(
        tmp_path / "heals" / "7",
        captured_heal.evidence.dom.value,
        nodes={
            hostile: NodeBox(node_id=captured_heal.node_id, bbox=BBox(x=0, y=0, width=1, height=1))
        },
    )
    replay_bundle(
        bundle,
        CLAIM_NUMBER,
        captured_heal.cache,
        ExpectedHeal(captured_heal.winning_rung, captured_heal.node_id),
    )
    assert sorted(p.name for p in bundle.iterdir()) == sorted([AFTER_HTML, AFTER_PNG, MANIFEST])
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == sorted(
        [AFTER_HTML, AFTER_PNG, MANIFEST]
    )
    assert hostile in read_heal_manifest(bundle).after.nodes


@pytest.mark.unit
def test_replay_resolving_a_different_node_is_not_reproducible(
    captured_heal: CapturedHeal, tmp_path: Path
) -> None:
    bundle = write_test_bundle(tmp_path / "heals" / "7", captured_heal.evidence.dom.value)
    with pytest.raises(NotReproducible, match="different element"):
        replay_bundle(
            bundle,
            CLAIM_NUMBER,
            captured_heal.cache,
            ExpectedHeal(captured_heal.winning_rung, "/html/body/p[99]"),
        )


@pytest.mark.unit
def test_replay_won_by_a_different_rung_is_not_reproducible(
    captured_heal: CapturedHeal, tmp_path: Path
) -> None:
    bundle = write_test_bundle(tmp_path / "heals" / "7", captured_heal.evidence.dom.value)
    other = 4 if captured_heal.winning_rung != 4 else 3
    with pytest.raises(NotReproducible, match="won on replay"):
        replay_bundle(
            bundle, CLAIM_NUMBER, captured_heal.cache, ExpectedHeal(other, captured_heal.node_id)
        )


@pytest.mark.integration
def test_replay_writes_no_resolutions_row(captured_heal: CapturedHeal) -> None:
    """Found 2026-09-26. Replays are not scheduled resolutions; counting them
    would inflate the published rung shares (C4's denominator)."""
    with Session() as s:
        before = s.query(Resolution).count()
    replay(captured_heal.evidence, CLAIM_NUMBER, captured_heal.cache)
    with Session() as s:
        assert s.query(Resolution).count() == before


@pytest.mark.unit
def test_heallog_cannot_import_browser() -> None:
    r = subprocess.run(
        ["uv", "run", "lint-imports", "--contract", "heallog-cannot-refetch"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


# -- fix round 1, R1: a live heal's node id is a data-sd-node token, replay's is an lxml path --


def _stamped(html: str, token_on: str, *, twice: bool = False) -> str:
    """Every element stamped the way LivePageSource stamps a live page, with `n17` on the
    element at lxml path `token_on` (and, with twice=True, on its next cell as well)."""
    import lxml.html

    root = lxml.html.document_fromstring(html)
    tree = root.getroottree()
    for i, el in enumerate(root.iter()):
        if isinstance(el.tag, str):
            el.set("data-sd-node", f"n{100 + i}")
    target = root.xpath(token_on)[0]
    target.set("data-sd-node", "n17")
    if twice:
        target.getnext().set("data-sd-node", "n17")
    assert tree.getpath(target) == token_on
    return lxml.html.tostring(root, encoding="unicode", doctype="<!doctype html>")


@pytest.mark.unit
def test_a_live_token_resolving_to_the_replayed_cell_reproduces(
    captured_heal: CapturedHeal, tmp_path: Path
) -> None:
    html = _stamped(captured_heal.evidence.dom.value, captured_heal.node_id)
    bundle = write_test_bundle(tmp_path / "heals" / "7", html)
    el, trace = replay_bundle(
        bundle, CLAIM_NUMBER, captured_heal.cache, ExpectedHeal(captured_heal.winning_rung, "n17")
    )
    assert trace.winner == captured_heal.winning_rung and el.node_id == captured_heal.node_id


@pytest.mark.unit
def test_a_live_token_on_a_different_cell_is_not_reproducible(
    captured_heal: CapturedHeal, tmp_path: Path
) -> None:
    neighbour = captured_heal.node_id.replace("td[1]", "td[2]")
    html = _stamped(captured_heal.evidence.dom.value, neighbour)
    bundle = write_test_bundle(tmp_path / "heals" / "7", html)
    with pytest.raises(NotReproducible, match="different element"):
        replay_bundle(
            bundle,
            CLAIM_NUMBER,
            captured_heal.cache,
            ExpectedHeal(captured_heal.winning_rung, "n17"),
        )


@pytest.mark.unit
@pytest.mark.parametrize("token", ["n99999", "n17-twice"])
def test_a_live_token_absent_or_duplicated_in_the_stored_dom_is_corrupted(
    captured_heal: CapturedHeal, tmp_path: Path, token: str
) -> None:
    html = _stamped(captured_heal.evidence.dom.value, captured_heal.node_id, twice=True)
    bundle = write_test_bundle(tmp_path / "heals" / "7", html)
    with pytest.raises(EvidenceCorrupted):
        replay_bundle(
            bundle,
            CLAIM_NUMBER,
            captured_heal.cache,
            ExpectedHeal(captured_heal.winning_rung, token.removesuffix("-twice")),
        )
