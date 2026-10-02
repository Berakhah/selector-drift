"""Task 19 fix round 1 (R2 / S-I4): the only screenshot the product takes is LiveCapture's.

A screenshot anywhere else would carry no credential floor, no live default-deny mask and no
Sensitivity label -- a bypass of never-rules 4 and 6 on the type every caller holds. Enforced by
an AST walk, in the style of test_playwright_page_boundary.py.
"""

import ast
from pathlib import Path

import pytest

from selector_drift.browser.driver import PageHandle

PACKAGE = Path(__file__).resolve().parents[1] / "selector_drift"
SANCTIONED = PACKAGE / "browser" / "live_capture.py"


def _screenshot_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "screenshot"
    ]


@pytest.mark.unit
def test_only_live_capture_takes_a_screenshot() -> None:
    sources = sorted(PACKAGE.rglob("*.py"))
    assert len(sources) > 10, f"scanned {len(sources)} files under {PACKAGE} -- vacuous"
    offenders = {
        f"{p.relative_to(PACKAGE)}:{line}"
        for p in sources
        if p != SANCTIONED
        for line in _screenshot_calls(p)
    }
    assert not offenders, f"a screenshot is taken outside browser/live_capture.py: {offenders}"
    assert _screenshot_calls(SANCTIONED), "the guard would pass vacuously"


@pytest.mark.unit
def test_a_page_handle_offers_no_screenshot() -> None:
    assert not hasattr(PageHandle, "screenshot")
