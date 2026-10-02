"""C2, structurally: a cache row is constructed in exactly one module."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ALLOWED = {Path("selector_drift/orchestrate/cache_writer.py"), Path("selector_drift/db/models.py")}


def _constructs_cache_row(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "SelectorCacheEntry")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "SelectorCacheEntry")
        )
        for n in ast.walk(tree)
    )


@pytest.mark.unit
def test_only_the_cache_writer_constructs_a_cache_row() -> None:
    """A second write site is a second way to spell 'write the cache' without first spelling
    'the gate passed' -- the one ordering C2 exists to make unspellable."""
    offenders = sorted(
        str(p)
        for p in Path("selector_drift").rglob("*.py")
        if p not in ALLOWED and _constructs_cache_row(p)
    )
    assert offenders == []
