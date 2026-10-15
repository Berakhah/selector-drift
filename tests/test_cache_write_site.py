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


READERS = {Path("selector_drift/resolve/cache.py")}  # may name the model, only inside select()
READ_BUILDERS = {"select", "where", "order_by", "limit", "desc", "asc"}  # query shaping only
WRITE_VERBS = ("insert", "update", "delete", "truncate")


def _names_cache_model(node: ast.AST) -> bool:
    return any(
        (isinstance(n, ast.Name) and n.id == "SelectorCacheEntry")
        or (isinstance(n, ast.Attribute) and n.attr == "SelectorCacheEntry")
        for n in ast.walk(node)
    )


@pytest.mark.unit
def test_no_other_module_writes_the_cache_table_by_any_spelling() -> None:
    """Construction is one spelling. `insert(SelectorCacheEntry)`, `update(...)`, `delete(...)`,
    `session.merge(...)` and raw SQL are the others (fix round 1, M1): outside the writer and
    the model, the model may be passed only to read-shaping calls (select, where, order_by, ...;
    and only in resolve/cache.py), and no SQL string pairs the table's name with a write verb."""
    offenders: list[str] = []
    for path in sorted(Path("selector_drift").rglob("*.py")):
        if path in ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                args = [*node.args, *(k.value for k in node.keywords)]
                func = ast.unparse(node.func).rsplit(".", 1)[-1]
                if any(_names_cache_model(a) for a in args) and (
                    path not in READERS or func not in READ_BUILDERS
                ):
                    offenders.append(f"{path}:{node.lineno} passes the cache model to {func}()")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.lower()
                if "selector_cache" in text and any(v in text for v in WRITE_VERBS):
                    offenders.append(f"{path}:{node.lineno} writes the cache table in SQL text")
    assert offenders == []


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
