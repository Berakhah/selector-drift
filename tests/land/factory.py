"""Builds real ValidatedExtractions through the real gate -- the lake refuses anything else, and a
test that minted one by hand would be testing a door nobody else can use."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from bok_core.provenance.claim import Claim, Method
from bok_core.provenance.source import DomSource

from selector_drift.config import get_settings
from selector_drift.extract.models import CandidateExtraction
from selector_drift.land.catalog import QueryResult
from selector_drift.resolve.cache import CachedSelector
from selector_drift.spec.schema import FieldTarget
from selector_drift.validate.gate import HealContext, ValidatedExtraction, gate_run
from selector_drift.validate.verdict import Outcome, Verdict

RUN_KEY = "testbench:2:2026-09-07"
AT = dt.datetime(2026, 9, 7, 4, 12, tzinfo=dt.UTC)
SNAPSHOT = "e5" * 32
AMOUNTS = [100 + 7.5 * i for i in range(100)]

SPEC_YAML = r"""version: 2
scope_ref: scopes/testbench.yaml
schedule: "12 4 * * *"
row_scope: each row of the results table
natural_key: [ claim_number ]
expected_schema:
  claim_number: string
  amount: decimal
navigate: []
targets:
  - name: claim_number
    description: the claim number cell in the results table
    assertions:
      - { type: format, pattern: '^[A-Z]{2}\d{8}$' }
  - name: amount
    description: the claimed amount in the same row
    distributional: true
    assertions:
      - { type: format, pattern: '^\d{1,3}(,\d{3})*\.\d{2}$' }
"""


def configure_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    (specs / "testbench.yaml").write_text(SPEC_YAML, encoding="utf-8")
    monkeypatch.setenv("SD_LAKE_ROOT", str(tmp_path / "lake"))
    monkeypatch.setenv("SD_SPEC_DIR", str(specs))
    get_settings.cache_clear()
    return tmp_path / "lake"


def rows(
    n: int = 100, *, scale: float = 1.0, blank_amounts: int = 0, extra: dict[str, str] | None = None
) -> list[dict[str, str]]:
    out = []
    for i in range(n):
        r = {
            "claim_number": f"AB{10_000_000 + i:08d}",
            "amount": "" if i < blank_amounts else f"{AMOUNTS[i % 100] * scale:,.2f}",
        }
        out.append(r | (extra or {}))
    return out


def claim(value: str, field: str) -> Claim:  # type: ignore[type-arg]
    return Claim(
        value=value,
        method=Method.DETERMINISTIC,
        confidence=None,
        observed_at=AT,
        source=DomSource(snapshot_id=SNAPSHOT, selector=f"td.{field}", rung=1),
    )


def candidate(plain: list[dict[str, str]], run_key: str = RUN_KEY) -> CandidateExtraction:
    return CandidateExtraction(
        rows=[{f: claim(v, f) for f, v in r.items()} for r in plain],
        trace={},
        spec_version=int(run_key.split(":")[1]),
        run_key=run_key,
    )


class AlwaysPass:
    name = "AlwaysPass"

    def check(self, candidate: object, history: object, ctx: object) -> Verdict:
        return Verdict(
            check=self.name,
            outcome=Outcome.PASS,
            threshold=None,
            observed=None,
            reason="test check",
        )


class _NoCache:
    def get(
        self, target: str, field: str, spec_version: int | None = None
    ) -> CachedSelector | None:
        return None


def _target(name: str) -> FieldTarget:
    return FieldTarget(name=name, description=f"the {name} cell", relation=None, assertions=[])


def make_validated(plain: list[dict[str, str]], run_key: str = RUN_KEY) -> ValidatedExtraction:
    # gate_run, not gate: the real gate refuses to mint a run carrying a field it never judged,
    # so every field in the rows gets a context and a check.
    fields = sorted({f for r in plain for f in r})
    contexts = {
        f: HealContext(healed=False, winning_rung=1, page=None, target=_target(f), cache=_NoCache())
        for f in fields
    }
    result = gate_run(
        candidate(plain, run_key), None, contexts, {f: [AlwaysPass()] for f in fields}
    )
    assert isinstance(result, ValidatedExtraction)
    return result


def parquet_files(lake: Path) -> list[Path]:
    return sorted(lake.glob("target=*/window=*/*.parquet"))


def scalar(result: QueryResult) -> Any:
    """The single value of a one-row, one-column result."""
    row = result.fetchone()
    assert row is not None
    return row[0]
