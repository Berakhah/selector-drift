"""The bench's day-over-day churn as the GATE sees it: the rendered results page, read by the real
extraction path, joined by RowKeyContinuity itself (Task 26B).

tests/testbench/test_data.py measures agreement over the whole dataset, which is mostly closed
claims that never change; the page shows the newest 50, almost all live. Before Task 26B the delta
drew 2% of ALL claims but applied every change to the ~5% that are live, so ~40% of live claims
moved each day and every benign heal of `amount` or `status` was refused -- while the dataset-wide
test stayed green. This guard measures the population the gate actually judges.

Bench spec section 4: "on a benign day ~98% of keys agree, above its 0.95 threshold". With 45
joined rows, three changes in one field (0.933) on a given day is the Poisson tail of a ~1% daily
per-field rate -- and for `status` the 2-4 forced daily closures add to that rate -- not a defect.
So "every day" is asserted only for the days the regression suite judges, and the window is
bounded: mean >= 0.98, never below 0.90, at most 2 of 60 days below 0.95.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from selector_drift.extract.models import CandidateExtraction
from selector_drift.extract.reader import read
from selector_drift.resolve.cascade import resolve_cascade
from selector_drift.resolve.page import StaticPageSource
from selector_drift.spec.loader import load_spec
from selector_drift.validate.assertions.independence import RowKeyContinuity
from selector_drift.validate.history import AcceptedRun
from testbench.portal.render import render_results
from tests.suite.conftest import BASE_DAY

SPEC = load_spec(Path(__file__).resolve().parents[2] / "specs" / "testbench.yaml")
FIELDS = [t.name for t in SPEC.targets]
AT = dt.datetime(2026, 10, 1, 4, 12, tzinfo=dt.UTC)
THRESHOLD = RowKeyContinuity().threshold  # 0.95, the heal threshold -- read, not restated
START = dt.date(2026, 9, 1)
DAYS = 60
SUITE_DAYS = [BASE_DAY - dt.timedelta(days=6 - i) for i in range(7)]  # tests/suite's history + base
_PAGES: dict[dt.date, CandidateExtraction] = {}


class _NoCache:
    def get(self, target: str, field: str, spec_version: int | None = None) -> None:
        return None


def _page(day: dt.date) -> CandidateExtraction:
    if day not in _PAGES:
        html = render_results(day)
        source = StaticPageSource(html)
        resolved = {
            t.name: resolve_cascade(source, t, _NoCache(), record=False)[0] for t in SPEC.targets
        }
        _PAGES[day] = read(
            html,
            resolved,
            SPEC,
            run_key=f"testbench:2:{day}",
            snapshot_id="0" * 64,
            traces={},
            observed_at=AT,
        )
    return _PAGES[day]


def _continuity(day: dt.date) -> dict[str, tuple[float, int]]:
    """Each field's RowKeyContinuity agreement on `day`'s page against the day before's."""
    before = _page(day - dt.timedelta(days=1))
    previous = AcceptedRun(
        run_key=before.run_key,
        window=day - dt.timedelta(days=1),
        rows=tuple({k: str(c.value) for k, c in row.items()} for row in before.rows),
        natural_key=tuple(SPEC.natural_key),
    )
    check = RowKeyContinuity()
    return {f: check.agreement(_page(day), previous, f) for f in FIELDS}


@pytest.fixture(scope="module")
def window() -> list[tuple[dt.date, dict[str, tuple[float, int]]]]:
    return [(d, _continuity(d)) for d in (START + dt.timedelta(days=i + 1) for i in range(DAYS))]


@pytest.mark.unit
@pytest.mark.parametrize("field", FIELDS)
def test_page_continuity_averages_the_spec_98_percent(window, field: str) -> None:
    rates = [c[field][0] for _, c in window]
    assert sum(rates) / len(rates) >= 0.98, f"{field}: mean {sum(rates) / len(rates):.3f}"


@pytest.mark.unit
@pytest.mark.parametrize("field", FIELDS)
def test_page_continuity_never_falls_far_and_rarely_below_the_heal_threshold(
    window, field: str
) -> None:
    rates = {d: c[field] for d, c in window}
    assert all(n >= 40 for _, n in rates.values()), "the page must join ~45 rows a day"
    worst = min(rates.items(), key=lambda kv: kv[1][0])
    assert worst[1][0] >= 0.90, f"{field}: {worst[1][0]:.3f} on {worst[0]}"
    below = sorted(str(d) for d, (r, _) in rates.items() if r < THRESHOLD)
    assert len(below) <= 2, f"{field}: below {THRESHOLD} on {len(below)} of {DAYS} days: {below}"


@pytest.mark.unit
@pytest.mark.parametrize("day", SUITE_DAYS[1:], ids=str)
def test_every_day_the_regression_suite_judges_is_above_the_heal_threshold(day: dt.date) -> None:
    low = {f: round(r, 3) for f, (r, _) in _continuity(day).items() if r < THRESHOLD}
    assert low == {}, f"{day}: {low}"
