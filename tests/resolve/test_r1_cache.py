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
from selector_drift.spec.schema import FieldTarget, matches
from testbench.mutations.adversarial.hidden_decoy import HiddenDecoy
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
    cache = warm(reworded.html, "claim_number")  # header_text anchor, set by rung 4
    cached = cache.get("testbench", "claim_number", 2)
    assert cached is not None
    header = cached.anchor.header_text
    assert header is not None
    twice = StaticPageSource(reworded.html.replace("</tr>", f"<th>{header}</th></tr>", 1))
    [_el] = twice.query(cached.selector)  # the selector still names one cell
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


# --- The adversarial checklist (Task 17 dispatch): rung 1 must never return a wrong element. ---

ROW1 = "html > body > main > table > tbody > tr:nth-of-type(1)"


def entry(field: str, selector: str, anchor: AnchorEvidence, by: int = 2) -> DictCache:
    return DictCache({("testbench", field, 2): CachedSelector(selector, anchor, by)})


def truth_or_inside(page: StaticPageSource, node_id: str, field: str) -> bool:
    """The resolved node is the truth cell, or an element inside it (the claim-number <a>)."""
    node = page.node(node_id)
    assert node is not None
    return any((n.get("data-sd-truth") or "") == field for n in [node, *node.iterancestors()])


@pytest.mark.unit
def test_a_decoy_column_makes_a_cloned_class_selector_ambiguous(bench: BenchPage) -> None:
    """DecoyColumn clones the real column's class: the cached class selector now matches two."""
    cache = entry("amount", f"{ROW1} > td.col-amount", AnchorEvidence(accessible_name="Amount"))
    assert rung(cache).resolve(bench.source(), AMOUNT) is not None  # one match before
    got = rung(cache).attempt(bench.mutated("DecoyColumn").source(), AMOUNT)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


@pytest.mark.unit
@pytest.mark.parametrize(
    "anchor",
    [AnchorEvidence(accessible_name="Amount"), AnchorEvidence(header_text="Amount")],
    ids=["accessible_name", "header_text"],
)
def test_a_positional_selector_landing_on_the_decoy_column_misses(
    bench: BenchPage, anchor: AnchorEvidence
) -> None:
    """The decoy is inserted before Amount, so `td:nth-of-type(3)` now names the decoy cell,
    whose value passes the amount format. Only the anchor can object -- and does."""
    cache = entry("amount", f"{ROW1} > td:nth-of-type(3)", anchor, by=4)
    assert rung(cache).resolve(bench.source(), AMOUNT) is not None
    decoyed = bench.mutated("DecoyColumn")
    page = decoyed.source()
    [cell] = page.query(f"{ROW1} > td:nth-of-type(3)")
    assert matches(AMOUNT, " ".join(cell.text_content().split()))
    assert page.node_id(cell) not in decoyed.truth("amount")
    got = rung(cache).attempt(page, AMOUNT)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch"


@pytest.mark.unit
@pytest.mark.parametrize("variant", ["aria-hidden", "display-none"])
@pytest.mark.parametrize(
    "anchor",
    [AnchorEvidence(accessible_name="Claim Number"), AnchorEvidence(header_text="Claim Number")],
    ids=["accessible_name", "header_text"],
)
def test_a_hidden_decoy_is_never_a_hit(
    bench: BenchPage, variant: str, anchor: AnchorEvidence
) -> None:
    """Every hidden element on a HiddenDecoy page, cached under a claim-number anchor: none
    is a hit, and the visible truth cell still is."""
    decoyed = bench.mutated_by(HiddenDecoy(variant=variant))
    page = decoyed.source()
    [body] = page.query("body")
    hidden = [e for e in body.iter() if isinstance(e.tag, str) and not page.visible(e)]
    assert hidden
    for el in hidden:
        got = rung(entry("claim_number", css_path(el), anchor)).attempt(page, CLAIM_NUMBER)
        assert isinstance(got, Miss), css_path(el)
    [cell] = page.query(decoyed.truth("claim_number")[0])
    hit = rung(entry("claim_number", css_path(cell), anchor)).attempt(page, CLAIM_NUMBER)
    assert isinstance(hit, ResolvedElement)


@pytest.mark.unit
def test_value_swap_is_a_hit_on_the_original_node_and_left_to_the_gate(
    bench: BenchPage, warm_cache: DictCache
) -> None:
    """ValueSwap: markup untouched, Amount and Reserve text exchanged. Rung 1 rightly returns
    the Amount cell -- it judges position and format, not the number; the gate's
    RowKeyContinuity check (Task 22) owns this one."""
    swapped = bench.mutated("ValueSwap")
    page = swapped.source()
    got = rung(warm_cache).attempt(page, AMOUNT)
    assert isinstance(got, ResolvedElement) and got.node_id == swapped.truth("amount")[0]
    [was_reserve] = bench.source().query(bench.truth("reserve")[0])
    node = page.node(got.node_id)
    assert node is not None and node.text_content().strip() == was_reserve.text_content().strip()


@pytest.mark.unit
def test_a_locale_flip_fails_the_format_and_misses(bench: BenchPage, warm_cache: DictCache) -> None:
    got = rung(warm_cache).attempt(bench.mutated("LocaleFlip").source(), AMOUNT)
    assert isinstance(got, Miss) and got.outcome == "no_match" and "format" in got.detail


@pytest.mark.unit
def test_a_label_reword_misses_rung_one_and_rung_four_heals_correctly(
    bench: BenchPage, warm_cache: DictCache
) -> None:
    reworded = bench.mutated("LabelReword")
    page = reworded.source()
    for field in (CLAIM_NUMBER, AMOUNT, RESERVE):
        got = rung(warm_cache).attempt(page, field)
        assert isinstance(got, Miss) and got.outcome == "anchor_mismatch", field.name
        el, trace = resolve_cascade(
            page, field, warm_cache, record=False, run_key="testbench:2:2026-10-01"
        )
        assert trace.winner == 4 and trace.healed
        assert el.node_id == reworded.truth(field.name)[0]


HAND = """<html><body><main><table>
<thead>{band}</thead>
{tfoot}
<tbody>
<tr><td>AB12345678</td><td>1,000.00</td></tr>
<tr><th>Claim Number</th><th>Amount</th></tr>
<tr><td>CD12345678</td><td>2,000.00</td></tr>
</tbody>
</table></main></body></html>"""
BAND = "<tr><th>Claim Number</th><th>Amount</th></tr>"
ROWSPAN_BAND = '<tr><th rowspan="2">Claim Number</th><th>Money</th></tr><tr><th>Amount</th></tr>'
TFOOT = "<tfoot><tr><td>ZZ99999999</td><td>3,000.00</td></tr></tfoot>"
FIRST = "html > body > main > table > tbody > tr:nth-of-type(1) > td:nth-of-type(1)"


def hand(band: str = BAND, tfoot: str = "") -> StaticPageSource:
    return StaticPageSource(HAND.format(band=band, tfoot=tfoot))


ANCHORS = [
    AnchorEvidence(accessible_name="Claim Number"),
    AnchorEvidence(label_text="Claim Number"),
    AnchorEvidence(header_text="Claim Number"),
]


@pytest.mark.unit
@pytest.mark.parametrize("anchor", ANCHORS, ids=["name", "label", "header"])
def test_a_cached_selector_hits_only_the_first_record_cell(anchor: AnchorEvidence) -> None:
    """A repeated mid-table header, a later record, a <tfoot> placed first, a band header:
    a cached selector naming any of them is never a hit; the first record's cell is."""
    page = hand(tfoot=TFOOT)
    hit = rung(entry("claim_number", FIRST, anchor)).attempt(page, CLAIM_NUMBER)
    assert isinstance(hit, ResolvedElement), hit
    for wrong in (
        "html > body > main > table > thead > tr > th:nth-of-type(1)",  # the band header
        "html > body > main > table > tbody > tr:nth-of-type(2) > th:nth-of-type(1)",  # repeat
        "html > body > main > table > tbody > tr:nth-of-type(3) > td:nth-of-type(1)",  # record 2
        "html > body > main > table > tfoot > tr > td:nth-of-type(1)",  # totals, first in source
    ):
        got = rung(entry("claim_number", wrong, anchor)).attempt(page, CLAIM_NUMBER)
        assert isinstance(got, Miss), (anchor, wrong)


@pytest.mark.unit
@pytest.mark.parametrize("anchor", ANCHORS[::2], ids=["name", "header"])
def test_a_rowspan_header_band_never_hits(anchor: AnchorEvidence) -> None:
    """Column position cannot be trusted under a rowspanned band: rung 1 declines."""
    got = rung(entry("claim_number", FIRST, anchor)).attempt(hand(band=ROWSPAN_BAND), CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["ColumnReorder", "DecoyColumn", "DuplicateLabel", "ValueSwap"])
@pytest.mark.parametrize(
    ("field", "anchor"),
    [
        (CLAIM_NUMBER, AnchorEvidence(accessible_name="Claim Number")),
        (CLAIM_NUMBER, AnchorEvidence(header_text="Claim Number")),
        (AMOUNT, AnchorEvidence(accessible_name="Amount")),
        (AMOUNT, AnchorEvidence(header_text="Amount")),
    ],
    ids=["claim-name", "claim-header", "amount-name", "amount-header"],
)
def test_no_cached_selector_on_the_page_hits_anything_but_the_truth(
    bench: BenchPage, name: str, field: FieldTarget, anchor: AnchorEvidence
) -> None:
    """Exhaustive: EVERY element in the body, cached as the selector under a true anchor, on
    an adversarially mutated page. Any hit must be the truth cell (or inside it)."""
    page = StaticPageSource(bench.mutated(name).marked)
    [body] = page.query("body")
    for el in [e for e in body.iter() if isinstance(e.tag, str)]:
        got = rung(entry(field.name, css_path(el), anchor)).attempt(page, field)
        if isinstance(got, ResolvedElement):
            assert truth_or_inside(page, got.node_id, field.name), (name, css_path(el))
