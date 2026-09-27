from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from selector_drift.__main__ import main

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_run_dispatches_one_extraction_and_prints_its_key(monkeypatch, capsys) -> None:
    seen: list[str] = []

    def fake_flow(spec: str) -> str:
        seen.append(spec)
        return "testbench:1:2026-09-07"

    monkeypatch.setattr("selector_drift.orchestrate.flows.extraction_flow", fake_flow)
    assert main(["run", "testbench.yaml"]) == 0
    assert seen == ["testbench.yaml"]
    assert capsys.readouterr().out.strip() == "testbench:1:2026-09-07"


@pytest.mark.unit
def test_no_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


@pytest.mark.unit
def test_there_is_no_cli_framework_and_no_console_script() -> None:
    """Global constraint: no [project.scripts], no Typer/Click."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "scripts" not in project
    for path in (REPO / "selector_drift").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not {n.split(".")[0] for n in names} & {"typer", "click"}, path
