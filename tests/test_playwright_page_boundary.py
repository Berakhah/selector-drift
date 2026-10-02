"""Fix round 1 (S-I1): only browser/ may hold the raw Playwright page.

A raw page's goto()/click() skip ratelimit.acquire(target) and raise_if_off_scope(), so the
boundary is enforced here by an AST walk rather than left to a docstring.
"""

import ast
from pathlib import Path

import pytest

from selector_drift.browser.driver import PageHandle

PACKAGE = Path(__file__).resolve().parents[1] / "selector_drift"
BROWSER = PACKAGE / "browser"


def _reaches_raw_page(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("_page", "_playwright_page", "raw"):
            found.add(f"line {node.lineno}: .{node.attr}")
        elif isinstance(node, ast.Name) and node.id == "_playwright_page":
            found.add(f"line {node.lineno}: _playwright_page")
        elif isinstance(node, ast.alias) and node.name == "_playwright_page":
            found.add("import of _playwright_page")
    return found


@pytest.mark.unit
def test_only_browser_reaches_the_playwright_page() -> None:
    sources = [p for p in sorted(PACKAGE.rglob("*.py")) if BROWSER not in p.parents]
    assert len(sources) > 10, f"scanned {len(sources)} files under {PACKAGE} -- vacuous"
    offenders = {f"{p}: {hit}" for p in sources for hit in _reaches_raw_page(p)}
    assert not offenders, f"the raw Playwright page escaped browser/: {offenders}"


@pytest.mark.unit
def test_a_page_handle_has_no_public_raw_page() -> None:
    assert not hasattr(PageHandle, "raw")
