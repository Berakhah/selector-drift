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


def _lock_bok_core_package() -> dict[str, object]:
    packages = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    entries = [p for p in packages if p["name"] == "bok-core"]
    assert len(entries) == 1, f"expected exactly one bok-core in uv.lock, found {len(entries)}"
    entry: dict[str, object] = entries[0]
    return entry


@pytest.mark.unit
def test_uv_lock_has_no_absolute_path_and_references_the_vendored_wheel() -> None:
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    drive_letter_path = re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[/\\]", lock)
    assert not drive_letter_path, "uv.lock contains a drive-letter path"
    assert "backoffice-kit" not in lock, "uv.lock still points at the sibling repo"
    relative = _bok_core_source()["path"]
    entry = _lock_bok_core_package()
    # The bok-core *package entry* must source from the vendored wheel. A substring match
    # anywhere in the file would also pass on a `requires-dist` line while `source` pointed at a
    # registry or an editable checkout.
    assert entry["source"] == {"path": relative}, f"bok-core source is {entry['source']!r}"
    actual = hashlib.sha256(_wheel_path().read_bytes()).hexdigest()
    wheels = entry["wheels"]
    assert isinstance(wheels, list)
    assert [w["hash"] for w in wheels] == [f"sha256:{actual}"], (
        "uv.lock records a stale or extra hash for the vendored wheel"
    )


COPY_VENDOR = re.compile(r"^\s*COPY(\s+--\S+)*\s+vendor(\s|/)", re.IGNORECASE)
FROM_LINE = re.compile(r"^\s*FROM\s", re.IGNORECASE)


def _uv_syncs_without_vendor(dockerfile_text: str) -> tuple[int, list[int]]:
    """(number of `uv sync` lines, line numbers of those with no `COPY vendor` earlier in their
    own stage). A new FROM starts a stage with nothing copied yet."""
    syncs, missing, vendored = 0, [], False
    for number, raw in enumerate(dockerfile_text.splitlines(), start=1):
        if raw.lstrip().startswith("#"):
            continue
        if FROM_LINE.match(raw):
            vendored = False
        elif COPY_VENDOR.match(raw):
            vendored = True
        elif "uv sync" in raw:
            syncs += 1
            if not vendored:
                missing.append(number)
    return syncs, missing


@pytest.mark.unit
@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_dockerfile_copies_vendor_before_every_uv_sync(dockerfile: Path) -> None:
    syncs, missing = _uv_syncs_without_vendor(dockerfile.read_text(encoding="utf-8"))
    assert syncs, f"{dockerfile.name} has no `uv sync` line"
    assert not missing, f"{dockerfile.name}: `uv sync` before `COPY vendor` on line(s) {missing}"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("copy_line", "recognised"),
    [
        ("COPY vendor ./vendor", True),
        ("COPY vendor/ ./vendor/", True),
        ("COPY --chown=10001:10001 vendor ./vendor", True),
        ("COPY --link --chown=1:1 vendor ./vendor", True),
        ("COPY vendors ./x", False),
        ("COPY --from=build /vendor ./vendor", False),
    ],
)
def test_the_vendor_copy_matcher_handles_flags_and_rejects_lookalikes(
    copy_line: str, recognised: bool
) -> None:
    assert bool(COPY_VENDOR.match(copy_line)) is recognised


@pytest.mark.unit
def test_a_uv_sync_in_a_later_stage_without_vendor_is_caught() -> None:
    """Guard the guard: the first stage is fine, the second syncs without vendor."""
    text = (
        "FROM python:3.12 AS build\n"
        "COPY --chown=1:1 vendor ./vendor\n"
        "RUN uv sync --frozen\n"
        "FROM python:3.12\n"
        "RUN uv sync --frozen\n"
    )
    assert _uv_syncs_without_vendor(text) == (2, [5])


@pytest.mark.unit
def test_vendor_readme_records_the_wheels_real_sha256() -> None:
    readme = (ROOT / "vendor" / "README.md").read_text(encoding="utf-8")
    actual = hashlib.sha256(_wheel_path().read_bytes()).hexdigest()
    labelled = [ln for ln in readme.splitlines() if ln.lstrip("| ").startswith("sha256")]
    assert labelled, "vendor/README.md has no sha256 line"
    assert any(actual in ln for ln in labelled), (
        "the sha256 line in vendor/README.md does not match the wheel"
    )
