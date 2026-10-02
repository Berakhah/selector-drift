from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BENCH_ONLY = re.compile(
    r"x-testbench-|testbench_override_secret|sd-mutation-seed|data-sd-truth", re.IGNORECASE
)


@pytest.mark.unit
def test_the_product_contains_no_bench_specific_code() -> None:
    """Bench spec §3: the product never sends the override header and never reads the seed tag.
    The import contract stops it importing the bench; this stops it re-typing the bench."""
    offenders = [
        str(p.relative_to(REPO))
        for p in (REPO / "selector_drift").rglob("*.py")
        if BENCH_ONLY.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


@pytest.mark.unit
def test_the_bench_import_contracts_are_declared() -> None:
    text = (REPO / ".importlinter").read_text(encoding="utf-8")
    contracts = ("answer-key-isolation", "bench-stands-alone", "only-the-scorer-reads-the-product")
    for contract in contracts:
        assert f"[importlinter:contract:{contract}]" in text
    marker = "[importlinter:contract:answer-key-isolation]"
    isolation = text.split(marker)[1].split("[importlinter")[0]
    assert re.search(r"^forbidden_modules = testbench$", isolation, re.MULTILINE)
