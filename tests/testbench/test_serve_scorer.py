"""The scorer's own entrypoint (bench-scorer compose service). It builds the deployment from the
flow itself, because importing selector_drift.orchestrate.deployments would be an indirect path
to selector_drift.db (contract only-the-scorer-reads-the-product). These tests pin that the
duplicated name and cron cannot drift from the worker's declaration."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_the_deployment_agrees_with_the_one_the_worker_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from selector_drift.orchestrate.deployments import BENCH_SCORER
    from testbench.serve_scorer import scorer_deployment

    monkeypatch.chdir(REPO)
    d = scorer_deployment()
    assert d.name == BENCH_SCORER["name"]
    assert d.flow_name == "bench-scorer"
    (schedule,) = d.schedules
    assert schedule.schedule.cron == BENCH_SCORER["cron"]
    assert schedule.schedule.timezone == BENCH_SCORER["timezone"]
    assert d.entrypoint is not None
    assert d.entrypoint.replace("\\", "/") == BENCH_SCORER["entrypoint"]


@pytest.mark.unit
def test_main_serves_exactly_the_one_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    import testbench.serve_scorer as module

    served: list[object] = []
    monkeypatch.setattr(module, "serve", lambda *deployments: served.extend(deployments))
    monkeypatch.chdir(REPO)
    module.main()
    assert [d.name for d in served] == ["bench-scorer"]  # type: ignore[attr-defined]


@pytest.mark.unit
def test_it_imports_only_prefect_and_the_scorer() -> None:
    """The import contract counts indirect imports; a bench module may reach selector_drift.db
    only through testbench.score."""
    tree = ast.parse((REPO / "testbench" / "serve_scorer.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    allowed = {
        "__future__",
        "logging",
        "typing",
        "prefect",
        "prefect.deployments.runner",
        "prefect.schedules",
        "testbench.score",
    }
    assert imported <= allowed, imported - allowed
