"""B8: an unpinned browser turns a UI-change test into a browser-change test. The Python package
and the Chromium build it downloads move together, so both are pinned and both are checked."""

import json
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pyproject() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _installed_chromium_revision() -> str:
    """M-9 (final review): the previous version of this test compared pyproject's value to the
    literal "1234" -- a constant restated in the test, never checked against what playwright
    actually installs. `browsers.json`, shipped inside the installed `playwright` package's own
    driver directory, is the source of truth for which Chromium build `playwright install`
    fetches for the pinned playwright version; reading it ties the two together instead."""
    import playwright

    browsers_json = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    data = json.loads(browsers_json.read_text(encoding="utf-8"))
    (chromium,) = [b for b in data["browsers"] if b["name"] == "chromium"]
    revision = chromium["revision"]
    assert isinstance(revision, str)
    return revision


@pytest.mark.unit
def test_the_chromium_revision_is_pinned() -> None:
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    assert tool["selector-drift"]["playwright_chromium_revision"] == _installed_chromium_revision()


@pytest.mark.unit
def test_the_installed_playwright_is_exactly_the_pinned_one() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    pins = [d for d in project["dependencies"] if d.split("=")[0].strip() == "playwright"]
    assert pins == ["playwright==1.62.0"], pins
    assert version("playwright") == "1.62.0"
