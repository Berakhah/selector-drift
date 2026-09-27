"""B8: an unpinned browser turns a UI-change test into a browser-change test. The Python package
and the Chromium build it downloads move together, so both are pinned and both are checked."""

import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pyproject() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_the_chromium_revision_is_pinned() -> None:
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    assert tool["selector-drift"]["playwright_chromium_revision"] == "1234"


@pytest.mark.unit
def test_the_installed_playwright_is_exactly_the_pinned_one() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    pins = [d for d in project["dependencies"] if d.split("=")[0].strip() == "playwright"]
    assert pins == ["playwright==1.62.0"], pins
    assert version("playwright") == "1.62.0"
