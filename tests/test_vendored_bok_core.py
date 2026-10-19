"""P-1: bok-core is a vendored wheel referenced by a repo-relative path, so a container image
(or any clean checkout) can `uv sync --frozen`. An absolute host path, or an editable path
into a sibling repo, cannot exist inside the worker or bench-scorer image."""

from __future__ import annotations

import fnmatch
import hashlib
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = [ROOT / "Dockerfile", ROOT / "docker" / "Dockerfile.bench-scorer"]
WHEEL_GLOB = "bok_core-*.whl"


def _bok_core_source() -> dict[str, object]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sources = pyproject["tool"]["uv"]["sources"]
    assert "bok-core" in sources, "[tool.uv.sources] must define bok-core"
    source: dict[str, object] = sources["bok-core"]
    return source


def _wheel_path() -> Path:
    path = _bok_core_source()["path"]
    assert isinstance(path, str)
    return ROOT / path


@pytest.mark.unit
def test_bok_core_source_is_a_relative_non_editable_path_to_an_existing_wheel() -> None:
    source = _bok_core_source()
    path = source.get("path")
    assert isinstance(path, str), "bok-core source must be a path source"
    assert not re.match(r"^[A-Za-z]:", path), f"drive letter in {path!r}"
    assert not path.startswith(("/", "\\")), f"absolute path {path!r}"
    assert not source.get("editable"), "bok-core must not be an editable install"
    wheel = ROOT / path
    assert wheel.is_file(), f"{path} does not exist under the repo root"
    assert fnmatch.fnmatch(wheel.name, WHEEL_GLOB), f"{wheel.name} is not a bok_core wheel"


@pytest.mark.unit
def test_uv_lock_has_no_absolute_path_and_references_the_vendored_wheel() -> None:
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    drive_letter_path = re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[/\\]", lock)
    assert not drive_letter_path, "uv.lock contains a drive-letter path"
    assert "backoffice-kit" not in lock, "uv.lock still points at the sibling repo"
    relative = _bok_core_source()["path"]
    assert isinstance(relative, str)
    assert relative in lock, f"uv.lock does not reference {relative}"


@pytest.mark.unit
@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_dockerfile_copies_vendor_before_the_first_uv_sync(dockerfile: Path) -> None:
    lines = [
        "" if ln.lstrip().startswith("#") else ln
        for ln in dockerfile.read_text(encoding="utf-8").splitlines()
    ]
    copy_idx = [i for i, ln in enumerate(lines) if re.match(r"\s*COPY\s+vendor\b", ln)]
    sync_idx = [i for i, ln in enumerate(lines) if "uv sync" in ln]
    assert copy_idx, f"{dockerfile.name} has no `COPY vendor` line"
    assert sync_idx, f"{dockerfile.name} has no `uv sync` line"
    assert copy_idx[0] < sync_idx[0], "`COPY vendor` must precede the first `uv sync`"


@pytest.mark.unit
def test_vendor_readme_records_the_wheels_real_sha256() -> None:
    readme = (ROOT / "vendor" / "README.md").read_text(encoding="utf-8")
    actual = hashlib.sha256(_wheel_path().read_bytes()).hexdigest()
    assert actual in readme, "vendor/README.md does not state the wheel's sha256"
