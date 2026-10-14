"""S-M4 and manifest exactness (Task 19 deferred to Task 33): bundle reads are exact against
disk, follow no link and are size-capped."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from selector_drift.heallog import bundle
from selector_drift.heallog.bundle import (
    AFTER_HTML,
    AFTER_PNG,
    MANIFEST,
    EvidenceCorrupted,
    read_heal_manifest,
    verified_bytes,
)
from tests.heallog.conftest import rewrite_manifest, write_test_bundle

PAGE = "<html><body><p>ok</p></body></html>"


@pytest.mark.unit
def test_an_intact_bundle_reads(tmp_path: Path) -> None:
    b = write_test_bundle(tmp_path / "heals" / "1", PAGE)
    manifest = read_heal_manifest(b)
    assert verified_bytes(b, manifest.files, AFTER_HTML) == PAGE.encode()


@pytest.mark.unit
def test_an_unlisted_extra_file_is_corruption(tmp_path: Path) -> None:
    b = write_test_bundle(tmp_path / "heals" / "1", PAGE)
    (b / "before.html").write_text("<html>planted</html>")
    with pytest.raises(EvidenceCorrupted, match="exactly"):
        read_heal_manifest(b)


@pytest.mark.unit
def test_a_manifest_without_after_png_is_corruption(tmp_path: Path) -> None:
    b = write_test_bundle(tmp_path / "heals" / "1", PAGE)
    data = {AFTER_HTML: bundle.file_digest(PAGE.encode())}
    rewrite_manifest(b, files=data)
    (b / AFTER_PNG).unlink()
    with pytest.raises(EvidenceCorrupted):
        read_heal_manifest(b)


@pytest.mark.unit
def test_a_listed_file_missing_from_disk_is_corruption(tmp_path: Path) -> None:
    b = write_test_bundle(tmp_path / "heals" / "1", PAGE)
    (b / AFTER_PNG).unlink()
    with pytest.raises(EvidenceCorrupted):
        read_heal_manifest(b)


@pytest.mark.unit
def test_a_before_half_needs_both_files(tmp_path: Path) -> None:
    b = write_test_bundle(tmp_path / "heals" / "1", PAGE)
    files = read_heal_manifest(b).files | {"before.html": bundle.file_digest(b"x")}
    rewrite_manifest(b, files=files)
    with pytest.raises(EvidenceCorrupted):
        read_heal_manifest(b)


def _can_symlink(tmp_path: Path) -> bool:
    try:
        (tmp_path / "probe-link").symlink_to(tmp_path / "probe-target")
    except OSError:
        return False
    return True


@pytest.mark.unit
def test_a_symlinked_file_is_never_followed(tmp_path: Path) -> None:
    if not _can_symlink(tmp_path):
        pytest.skip("this account cannot create symlinks")
    secret = tmp_path / "outside.html"
    secret.write_text(PAGE)
    b = write_test_bundle(tmp_path / "heals" / "1", PAGE)
    (b / AFTER_HTML).unlink()
    (b / AFTER_HTML).symlink_to(secret)
    with pytest.raises(EvidenceCorrupted):
        read_heal_manifest(b)
    with pytest.raises(EvidenceCorrupted, match="link"):
        bundle.read_capped(b / AFTER_HTML, bundle.MAX_HTML_BYTES)


@pytest.mark.unit
def test_an_oversized_file_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = write_test_bundle(tmp_path / "heals" / "1", PAGE)
    monkeypatch.setattr(bundle, "MAX_HTML_BYTES", 10)
    manifest = read_heal_manifest(b)
    with pytest.raises(EvidenceCorrupted, match="cap"):
        verified_bytes(b, manifest.files, AFTER_HTML)


@pytest.mark.unit
def test_an_oversized_manifest_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = write_test_bundle(tmp_path / "heals" / "1", PAGE)
    monkeypatch.setattr(bundle, "MAX_MANIFEST_BYTES", os.path.getsize(b / MANIFEST) - 1)
    with pytest.raises(EvidenceCorrupted):
        read_heal_manifest(b)
