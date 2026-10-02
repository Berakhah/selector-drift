from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest

from selector_drift.browser.registry import url_for
from selector_drift.spec.loader import load_scope
from tests._day_one import blocked_on_day_one_values as _blocked_on_day_one_values

REPO = Path(__file__).resolve().parents[2]

# Deviation from the brief (Task 12, found while implementing): `scopes/testbench.yaml` still
# carries day-one placeholders (<BENCH-HOST>, <DAY-ONE-UTC>, ...) -- `load_scope` on it raises
# ScopeRefusal today (invalid date fields, origin host not strict), the same way it does for
# the other two day-one targets. Controller ruling for this task says not to fill those
# placeholders in; they are the user's to resolve on the P-4 VM. These two tests therefore use
# the same `blocked_on_day_one_values` gate as `tests/scope/test_records_load.py` and
# `tests/spec/test_loader.py`, rather than the brief's unconditional PASS -- consistent with
# the project's one existing mechanism for this exact situation, instead of inventing a second.
# Once P-4 fills in the real values, both this file's tests and the gate unskip together.


@pytest.mark.unit
@_blocked_on_day_one_values
def test_the_bench_scope_record_names_exactly_one_origin() -> None:
    assert len(load_scope(REPO / "scopes" / "testbench.yaml").origins) == 1


@pytest.mark.unit
@_blocked_on_day_one_values
def test_the_registry_points_inside_the_bench_scope_record() -> None:
    """Compared as scheme + host, never by string prefix: https://bench.example.org.evil
    starts with https://bench.example.org."""
    origin = urlsplit(load_scope(REPO / "scopes" / "testbench.yaml").origins[0])
    url = urlsplit(url_for("testbench"))
    assert url.scheme == "https"
    assert (url.scheme, url.netloc) == (origin.scheme, origin.netloc)
