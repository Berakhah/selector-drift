# tests/test_bok_core_allowlist.py
import ast
from pathlib import Path

import pytest

# One line per submodule a task legitimately needs. Task 29 adds "bok_core.findings";
# Task 33 adds "bok_core.boundary.redact" and "bok_core.boundary.egress".
ALLOWED = {"bok_core.boundary.sensitivity", "bok_core.provenance"}

# From this file, never the cwd: run from anywhere else, a cwd-relative glob finds nothing and
# the test passes vacuously.
PACKAGE = Path(__file__).resolve().parents[1] / "selector_drift"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return {m for m in found if m == "bok_core" or m.startswith("bok_core.")}


@pytest.mark.unit
def test_selector_drift_imports_only_allow_listed_bok_core_modules() -> None:
    sources = sorted(PACKAGE.rglob("*.py"))
    assert len(sources) > 10, f"scanned {len(sources)} files under {PACKAGE} -- vacuous"
    offenders = {
        f"{p}: {m}"
        for p in sources
        for m in _imports(p)
        if not any(m == a or m.startswith(a + ".") for a in ALLOWED)
    }
    assert not offenders, (
        f"bok_core import outside the allow-list (v1 makes zero model calls): {offenders}"
    )
