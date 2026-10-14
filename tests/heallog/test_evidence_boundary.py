"""Static guards on where evidence may travel (S-M2, S-M6, S-M7)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from selector_drift.browser.driver import PageHandle

PACKAGE = Path(__file__).resolve().parents[2] / "selector_drift"
_EVIDENCE_TYPES = ("Evidence", "RedactedEvidence", "Labelled")


def _decorated_with(fn: ast.FunctionDef | ast.AsyncFunctionDef, names: set[str]) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name in names:
            return True
    return False


@pytest.mark.unit
def test_no_flow_or_task_takes_or_returns_evidence() -> None:
    """S-M2: Prefect stores flow parameters as JSON and may persist results; dataclasses.asdict
    unwraps a Labelled. Evidence never crosses a flow or task boundary."""
    sources = sorted(PACKAGE.rglob("*.py"))
    assert len(sources) > 10
    offenders: list[str] = []
    flows = 0
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not _decorated_with(node, {"flow", "task"}):
                continue
            flows += 1
            annotations = [a.annotation for a in node.args.args + node.args.kwonlyargs]
            annotations.append(node.returns)
            for ann in annotations:
                if ann is not None and any(t in ast.unparse(ann) for t in _EVIDENCE_TYPES):
                    offenders.append(f"{path.name}:{node.name}")
    assert flows >= 2, "found no flows -- vacuous"
    assert not offenders, offenders


@pytest.mark.unit
def test_the_aria_snapshot_never_reaches_storage() -> None:
    """S-M7: browser_ax is unscrubbed (an aria snapshot can carry textbox values). Only the
    page source that takes it may name it; nothing in heallog/, land/ or api/ does."""
    holders = {
        p.relative_to(PACKAGE).as_posix()
        for p in PACKAGE.rglob("*.py")
        if "browser_ax" in p.read_text(encoding="utf-8")
    }
    assert holders == {"browser/page_source.py"}


@pytest.mark.unit
def test_page_content_is_labelled() -> None:
    """S-M6: the raw DOM the driver hands out carries its target's label."""
    assert "Labelled" in str(inspect.signature(PageHandle.content).return_annotation)
