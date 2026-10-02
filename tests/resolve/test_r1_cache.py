"""Rung 1: the cached selector, and the anchor that justified it, re-verified (design 5.2)."""

from __future__ import annotations

import lxml.html
import pytest

from selector_drift.resolve.cache import NO_CACHE, CachedSelector
from selector_drift.resolve.cascade import resolve_cascade
from selector_drift.resolve.page import StaticPageSource
from selector_drift.resolve.rungs.base import css_path
from selector_drift.resolve.rungs.r1_cache import CacheRung
from selector_drift.resolve.trace import AnchorEvidence, Family, Miss, ResolvedElement
from selector_drift.spec.schema import matches
from tests.resolve.conftest import AMOUNT, CLAIM_NUMBER, RESERVE, BenchPage


class DictCache:
    """A CacheReader keyed exactly as selector_cache is: (target, field, spec_version)."""

    def __init__(self, entries: dict[tuple[str, str, int], CachedSelector]) -> None:
        self.entries = entries

    def get(
        self, target: str, field: str, spec_version: int | None = None
    ) -> CachedSelector | None:
        hits = [
            (v, c)
            for (t, f, v), c in self.entries.items()
            if (t, f) == (target, field) and spec_version in (None, v)
        ]
        return max(hits, key=lambda h: h[0])[1] if hits else None


def warm(html: str, *fields: str) -> DictCache:
    """The cache after a clean run on `html`: what rungs 2-4 resolved, at spec version 2."""
    page = StaticPageSource(html)
    targets = {"claim_number": CLAIM_NUMBER, "amount": AMOUNT, "reserve": RESERVE}
    entries = {}
    for name in fields:
        el, _ = resolve_cascade(page, targets[name], NO_CACHE, record=False)
        entries[("testbench", name, 2)] = CachedSelector(el.selector, el.anchor, el.rung_id)
    return DictCache(entries)


def rung(cache: DictCache, spec_version: int = 2) -> CacheRung:
    return CacheRung(cache, portal="testbench", spec_version=spec_version)


@pytest.fixture
def warm_cache(bench: BenchPage) -> DictCache:
    return warm(bench.html, "claim_number", "amount", "reserve")


@pytest.mark.unit
def test_rung_one_has_no_family_and_is_never_a_confirmer() -> None:
    assert (CacheRung.rung_id, CacheRung.family) == (1, Family.NONE)


@pytest.mark.unit
def test_unchanged_page_is_a_cache_hit(bench: BenchPage, warm_cache: DictCache) -> None:
    el = rung(warm_cache).resolve(bench.source(), CLAIM_NUMBER)
    assert el is not None and el.rung_id == 1
    assert el.node_id == bench.truth("claim_number")[0]


@pytest.mark.unit
def test_cache_hit_requires_exactly_one_match(bench: BenchPage) -> None:
    """A selector matching two elements after a change is a failure, not a first match."""
    cache = DictCache(
        {
            ("testbench", "claim_number", 2): CachedSelector(
                "tbody td:nth-of-type(1)", AnchorEvidence(accessible_name="Claim Number"), 2
            )
        }
    )
    got = rung(cache).attempt(bench.source(), CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


@pytest.mark.unit
def test_column_reorder_fails_rung_one_even_though_the_selector_still_matches(
    bench: BenchPage, warm_cache: DictCache
) -> None:
    """Spec 5.2. The selector still matches exactly one element -- but the anchor has moved,
    so rung 1 MUST fail, so a heal is recorded, so the strict path in Task 20 engages.
    Without this, the adjacent-column failure is silent and permanent."""
    reordered = bench.mutated("ColumnReorder")
    page = reordered.source()
    moved = [
        f
        for f in (CLAIM_NUMBER, AMOUNT, RESERVE)
        if reordered.truth(f.name)[0] != bench.truth(f.name)[0]
    ]
    assert moved, "the reorder moved none of the three cached columns"
    for field in moved:
        cached = warm_cache.get("testbench", field.name, 2)
        assert cached is not None and len(page.query(cached.selector)) == 1
        got = rung(warm_cache).attempt(page, field)
        assert isinstance(got, Miss) and got.outcome == "anchor_mismatch", field.name


@pytest.mark.unit
def test_swapping_two_same_format_columns_is_caught_by_the_anchor_alone(
    bench: BenchPage, warm_cache: DictCache
) -> None:
    """The purest form of 5.2: Amount and Reserve trade places, the cached selector matches
    one element and its value passes the amount format -- only the anchor can object."""
    swapped = (
        bench.html.replace(">Amount</th>", ">@A</th>")
        .replace(">Reserve</th>", ">Amount</th>")
        .replace(">@A</th>", ">Reserve</th>")
    )
    page = StaticPageSource(swapped)
    cached = warm_cache.get("testbench", "amount", 2)
    assert cached is not None
    [el] = page.query(cached.selector)
    assert matches(AMOUNT, " ".join(el.text_content().split()))
    got = rung(warm_cache).attempt(page, AMOUNT)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch"


@pytest.mark.unit
def test_a_duplicated_label_breaks_the_anchor(bench: BenchPage, warm_cache: DictCache) -> None:
    """Task 11A DuplicateLabel: the anchor must still identify ONE column."""
    got = rung(warm_cache).attempt(bench.mutated("DuplicateLabel").source(), CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch"


@pytest.mark.unit
def test_a_header_text_anchor_is_reverified_too(bench: BenchPage) -> None:
    reworded = bench.mutated("LabelReword")
    cache = warm(reworded.html, "claim_number")  # rungs 2-3 fail on a reword; rung 4 healed it
    cached = cache.get("testbench", "claim_number", 2)
    assert cached is not None and cached.set_by_rung == 4
    header = cached.anchor.header_text
    assert header is not None and header != "Claim Number"
    assert rung(cache).resolve(reworded.source(), CLAIM_NUMBER) is not None
    again = StaticPageSource(reworded.html.replace(f">{header}</th>", ">Case Ref.</th>"))
    got = rung(cache).attempt(again, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch"


@pytest.mark.unit
def test_a_header_text_anchor_must_be_unique_among_the_headers(bench: BenchPage) -> None:
    """Ledger 2026-09-26 (DuplicateLabel): the stored header still heads this column, but a
    second header now reads the same -- rung 1 must refuse, so a heal happens and the gate
    judges it, instead of a silent hit on whichever column the selector happens to name."""
    reworded = bench.mutated("LabelReword")
    cache = warm(reworded.html, "claim_number")       # header_text anchor, set by rung 4
    cached = cache.get("testbench", "claim_number", 2)
    assert cached is not None and (header := cached.anchor.header_text) is not None
    twice = StaticPageSource(reworded.html.replace("</tr>", f"<th>{header}</th></tr>", 1))
    [el] = twice.query(cached.selector)                 # the selector still names one cell
    got = rung(cache).attempt(twice, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch" and "uniquely" in got.detail


@pytest.mark.unit
def test_a_cache_entry_for_another_spec_version_is_not_a_hit(
    bench: BenchPage, warm_cache: DictCache
) -> None:
    """M14. A spec edit changes what the field MEANS."""
    got = rung(warm_cache, spec_version=3).attempt(bench.source(), CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "no_match"


@pytest.mark.unit
def test_a_hidden_match_is_not_a_hit(bench: BenchPage, warm_cache: DictCache) -> None:
    page = StaticPageSource(
        bench.html.replace('<table class="results"', '<table style="display:none" class="results"')
    )
    got = rung(warm_cache).attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "not_visible"


@pytest.mark.unit
def test_a_value_that_fails_the_format_is_not_a_hit(
    bench: BenchPage, warm_cache: DictCache
) -> None:
    """M23: spec.matches(), from spec/ -- resolve/ may not import validate/."""
    first = bench.truth("claim_number")[0]
    page = StaticPageSource(bench.html)
    [cell] = page.query(first)
    for child in list(cell):
        cell.remove(child)
    cell.text = "pending"
    page = StaticPageSource(lxml.html.tostring(page.root(), encoding="unicode"))
    got = rung(warm_cache).attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "no_match" and "format" in got.detail


@pytest.mark.unit
def test_an_entry_with_no_anchor_evidence_never_hits(bench: BenchPage) -> None:
    [el] = bench.source().query(bench.truth("claim_number")[0])
    cache = DictCache(
        {("testbench", "claim_number", 2): CachedSelector(css_path(el), AnchorEvidence(), 4)}
    )
    got = rung(cache).attempt(bench.source(), CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch"


@pytest.mark.unit
def test_a_hit_returns_the_cached_selector_and_anchor_unchanged(
    bench: BenchPage, warm_cache: DictCache
) -> None:
    cached = warm_cache.get("testbench", "claim_number", 2)
    got = rung(warm_cache).attempt(bench.source(), CLAIM_NUMBER)
    assert cached is not None and isinstance(got, ResolvedElement)
    assert (got.selector, got.anchor) == (cached.selector, cached.anchor)


@pytest.mark.unit
def test_the_cascade_binds_rung_one_to_the_runs_cache_and_version(
    bench: BenchPage, warm_cache: DictCache
) -> None:
    calls: list[tuple[str, str, int | None]] = []

    class Spy(DictCache):
        def get(
            self, target: str, field: str, spec_version: int | None = None
        ) -> CachedSelector | None:
            calls.append((target, field, spec_version))
            return super().get(target, field, spec_version)

    spy = Spy(warm_cache.entries)
    _, trace = resolve_cascade(
        bench.source(), CLAIM_NUMBER, spy, record=False, run_key="testbench:2:2026-10-01"
    )
    assert calls == [("testbench", "claim_number", 2)] and trace.winner == 1
