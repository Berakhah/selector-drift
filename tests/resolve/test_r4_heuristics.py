from __future__ import annotations

import random
from collections import Counter

import pytest

from selector_drift.resolve.page import StaticPageSource
from selector_drift.resolve.rungs.r4_heuristics import (
    AFFIXES,
    TESTID_ATTRS,
    UNSTABLE_ID,
    HeuristicRung,
    header_holds,
    names_field,
)
from selector_drift.resolve.trace import Family, Miss, ResolvedElement
from testbench.mutations.base import apply_all
from testbench.mutations.catalogue import CATALOGUE
from testbench.mutations.class_rename import ClassRename
from testbench.mutations.label_reword import LabelReword
from tests.resolve.conftest import AMOUNT, CLAIM_NUMBER, RESERVE, SPEC, BenchPage


def _page(body: str) -> StaticPageSource:
    return StaticPageSource(f"<html><body>{body}</body></html>")


def _by_id(page: StaticPageSource, ident: str) -> str:
    return page.node_id(page.query(f"#{ident}")[0])


# -- the brief's tests -----------------------------------------------------------------------


@pytest.mark.unit
def test_rung_four_is_structural() -> None:
    assert (HeuristicRung.rung_id, HeuristicRung.family) == (4, Family.STRUCTURAL)


@pytest.mark.unit
def test_heuristics_heals_through_label_reword(bench: BenchPage) -> None:
    """The one benign class the text family cannot survive (design 5.6a)."""
    page = bench.mutated("LabelReword")
    el = HeuristicRung().resolve(page.source(), CLAIM_NUMBER)
    assert el is not None and el.node_id in page.truth("claim_number")


@pytest.mark.unit
def test_heuristics_heals_through_restyling(bench: BenchPage) -> None:
    page = bench.mutated("ClassRename")
    el = HeuristicRung().resolve(page.source(), CLAIM_NUMBER)
    assert el is not None and el.node_id in page.truth("claim_number")


@pytest.mark.unit
def test_heuristics_never_lands_on_another_column_after_a_reorder(bench: BenchPage) -> None:
    """Replaces the first draft's `ColumnReorder -> None`, which assumed rung 4 has a
    remembered column index to be wrong about. It has none: it reads attributes and headers
    on today's page. Its obligation under a reorder is never to return a wrong cell."""
    page = bench.mutated("ColumnReorder")
    el = HeuristicRung().resolve(page.source(), CLAIM_NUMBER)
    assert el is None or el.node_id in page.truth("claim_number")


@pytest.mark.unit
def test_header_verification_refuses_a_cell_that_sits_under_another_header() -> None:
    """Header verification is what stops a bare column index being a guess: the first data
    row's cell at the header's index spans in from the Claimant column."""
    page = _page("""<table><tr><th>Claimant</th><th>Claim Number</th><th>Amount</th></tr>
<tr><td colspan="2">Test Person One AB12345678</td><td>100.00</td></tr></table>""")
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch"


@pytest.mark.unit
def test_header_verification_honours_the_headers_attribute() -> None:
    page = _page("""<table><tr><th id="n">Claim Number</th><th id="a">Amount</th></tr>
<tr><td headers="a">100.00</td><td headers="n">AB12345678</td></tr></table>""")
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch"


@pytest.mark.unit
def test_a_test_id_and_an_agreeing_header_resolve() -> None:
    """Fix round 1 (controller ruling): replaces the brief's test-id-beats-a-header test."""
    page = _page("""<table><tr><th>Other</th><th>Claim Number</th></tr>
<tr><td>x</td><td data-testid="claim-number" id="v">CD23456789</td></tr></table>""")
    el = HeuristicRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("#v")[0])
    assert el.anchor.header_text == "Claim Number"


@pytest.mark.unit
def test_a_test_id_contradicting_a_verified_header_is_ambiguous() -> None:
    page = _page("""<table><tr><th>Claim Number</th><th>Other</th></tr>
<tr><td class="col-claim_number">AB12345678</td>
<td data-testid="claim-number" id="v">CD23456789</td></tr></table>""")
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


_DECOY_TABLE = (
    "<table><tr><th>Claim Number</th><th>Prior Amount</th><th>Amount</th></tr>"
    '<tr><td>AB12345678</td><td {decoy}>90.00</td><td id="real">100.00</td></tr></table>'
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "decoy", ['data-testid="amount"', 'class="col-amount"'], ids=["test-id", "class"]
)
def test_an_attribute_on_the_adjacent_decoy_never_beats_the_verified_header(decoy: str) -> None:
    """Fix round 1 probes 1-2: the attribute names the decoy column, the verified header the
    real one. Neither wins: a disagreement is ambiguous."""
    page = _page(_DECOY_TABLE.format(decoy=decoy))
    got = HeuristicRung().attempt(page, AMOUNT)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


@pytest.mark.unit
def test_a_search_box_named_for_the_field_never_beats_the_results_header() -> None:
    """Fix round 1 probe 3: a search input's query value is not the first record."""
    page = _page(
        '<form><input name="claim_number" value="CD87654321"></form>'
        "<table><tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn">AB12345678</td><td>100.00</td></tr></table>'
    )
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


@pytest.mark.unit
def test_an_attribute_inside_the_verified_cell_agrees_with_it() -> None:
    page = _page(
        "<table><tr><th>Claim Number</th></tr>"
        '<tr><td><span id="v" class="claim-number">AB12345678</span></td></tr></table>'
    )
    el = HeuristicRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == _by_id(page, "v")


@pytest.mark.unit
def test_two_headers_for_the_field_never_let_an_attribute_pick_one() -> None:
    page = _page(
        "<table><tr><th>Claim Number</th><th>Claim Number</th></tr>"
        '<tr><td>AB12345678</td><td class="claim-number">CD87654321</td></tr></table>'
    )
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        '<div data-testid="claim-number"><span>Claim Number</span><span>AB12345678</span></div>',
        '<div class="claim-number"><label>Ref</label><span>AB12345678</span></div>',
        '<p id="claim-number">Claim Number: AB12345678</p>',
    ],
    ids=["label-span", "label-element", "label-prefix"],
)
def test_an_attribute_element_holding_its_label_is_never_a_value(body: str) -> None:
    """Fix round 1 (reviewer Minor 2): the whole div would read 'Claim NumberAB12345678'."""
    assert HeuristicRung().resolve(_page(body), CLAIM_NUMBER) is None


@pytest.mark.unit
def test_a_generated_id_is_not_evidence() -> None:
    page = _page('<p id="claim-number-8f3a2c19">AB12345678</p>')
    assert HeuristicRung().resolve(page, CLAIM_NUMBER) is None


@pytest.mark.unit
def test_our_own_and_the_benchs_data_sd_attributes_are_never_read() -> None:
    page = _page('<p data-sd-truth="claim_number" data-sd-node="claim_number">AB12345678</p>')
    assert HeuristicRung().resolve(page, CLAIM_NUMBER) is None


@pytest.mark.unit
def test_two_headers_with_the_label_and_no_attribute_is_ambiguous() -> None:
    page = _page("""<table><tr><th>Claim Number</th><th>Claim Number</th></tr>
<tr><td>AB12345678</td><td>CD23456789</td></tr></table>""")
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


@pytest.mark.unit
def test_an_image_header_is_verified_by_its_accessible_text(bench: BenchPage) -> None:
    """Task 11A: header verification reads accessible text, not textContent."""
    page = bench.mutated("ImageHeader")
    html = page.html.replace("col-amount", "c9")  # force the header strategy
    source = StaticPageSource(html)
    el = HeuristicRung().resolve(source, AMOUNT)
    assert el is not None and el.node_id in page.truth("amount")
    assert el.anchor.header_text == "Amount"


@pytest.mark.unit
def test_header_text_is_recorded_whichever_strategy_won(bench: BenchPage) -> None:
    page = bench.mutated("LabelReword")
    el = HeuristicRung().resolve(page.source(), CLAIM_NUMBER)
    assert el is not None and el.anchor.header_text not in (None, "Claim Number")
    assert el.anchor.accessible_name is None and el.anchor.label_text is None


# Checklist item 5: every listed value is a case.
@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "names"),
    [
        ("claim-number", True),
        ("claimNumber", True),
        ("col-claim_number", True),
        ("claim_number_value", True),
        ("prior-claim-number", False),
        ("claim", False),
        ("claim-number-old", False),
        ("claim-number-8f3a2c19", False),
        ("ctl00_MainContent_claim_number", False),
        ("claim-col-number", False),
        ("", False),
    ],
)
def test_names_field_is_token_equality_not_substring(value: str, names: bool) -> None:
    assert names_field(value, ("claim", "number")) is names


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "names"),
    [
        ("amount", True),
        ("col-amount", True),
        ("prior-amount", False),
        ("amount-prior", False),
        ("amount2", False),
    ],
)
def test_names_field_for_amount(value: str, names: bool) -> None:
    assert names_field(value, ("amount",)) is names


@pytest.mark.unit
def test_a_field_whose_own_name_is_an_affix_word_still_matches() -> None:
    """The brief's filter-every-affix rule would make `total_value` unmatchable."""
    assert names_field("col-total-value", ("total", "value"))
    assert not names_field("col-total", ("total", "value"))


@pytest.mark.unit
def test_the_published_vocabulary() -> None:
    assert TESTID_ATTRS == ("data-testid", "data-test-id", "data-test", "data-qa", "data-cy")
    assert {"col", "cell", "field", "value"} <= AFFIXES
    for generated in ("claim-number-8f3a2c19", "ember123", ":r1:", "ctl00_MainContent_x"):
        assert UNSTABLE_ID.search(generated), generated
    for stable in ("claim-number", "claimNumber", "amount"):
        assert not UNSTABLE_ID.search(stable), stable


# -- checklist 1: DecoyColumn ------------------------------------------------------------------


@pytest.mark.unit
def test_two_cells_in_a_row_sharing_the_class_is_never_first_by_position() -> None:
    page = _page(
        "<table><tr><th>Ref</th><th>Prior</th><th>Now</th></tr>"
        '<tr><td>R1</td><td class="col-amount">90.00</td><td class="col-amount">100.00</td></tr>'
        "</table>"
    )
    got = HeuristicRung().attempt(page, AMOUNT)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


@pytest.mark.unit
def test_the_bench_decoy_column_never_yields_the_decoy(bench: BenchPage) -> None:
    """The decoy clones the Amount cells' classes, so the class strategy is ambiguous; the
    header strategy, verified, may still take the true Amount column -- never the decoy."""
    for seed in range(3):
        entry = next(e for e in CATALOGUE if e.name == "DecoyColumn")
        page = bench.mutated_by(entry.build(random.Random(seed)))
        got = HeuristicRung().attempt(page.source(), AMOUNT)
        assert not isinstance(got, ResolvedElement) or got.node_id in page.truth("amount")


@pytest.mark.unit
def test_the_bench_decoy_columns_class_match_alone_is_ambiguous(bench: BenchPage) -> None:
    page = bench.mutated("DecoyColumn")
    # Remove the header strategy's signal so only the attribute strategy speaks.
    html = page.html.replace(">Amount<", ">Sum<")
    got = HeuristicRung().attempt(StaticPageSource(html), AMOUNT)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


# -- checklist 2: DuplicateLabel ---------------------------------------------------------------


@pytest.mark.unit
def test_the_bench_duplicate_label_is_never_the_legacy_column(bench: BenchPage) -> None:
    page = bench.mutated("DuplicateLabel")
    got = HeuristicRung().attempt(page.source(), CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


# -- checklist 3: HiddenDecoy ------------------------------------------------------------------


@pytest.mark.unit
def test_the_bench_hidden_decoy_never_wins(bench: BenchPage) -> None:
    for variant in ("aria-hidden", "display-none"):
        from testbench.mutations.adversarial.hidden_decoy import HiddenDecoy

        page = bench.mutated_by(HiddenDecoy(variant=variant))
        el = HeuristicRung().resolve(page.source(), CLAIM_NUMBER)
        assert el is not None and el.node_id in page.truth("claim_number")


@pytest.mark.unit
@pytest.mark.parametrize(
    "wrapper",
    [
        '<div aria-hidden="true">{}</div>',
        '<div style="display:none">{}</div>',
        "<div hidden>{}</div>",
    ],
)
def test_a_hidden_only_match_reports_not_visible(wrapper: str) -> None:
    page = _page(wrapper.format('<span class="claim-number">AB12345678</span>'))
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "not_visible"


@pytest.mark.unit
def test_a_hidden_header_only_reports_not_visible() -> None:
    page = _page(
        '<table aria-hidden="true"><tr><th>Claim Number</th></tr><tr><td>AB12345678</td></tr>'
        "</table>"
    )
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "not_visible"


@pytest.mark.unit
def test_a_hidden_decoy_grid_ahead_of_a_visible_one_is_skipped() -> None:
    page = _page(
        '<div aria-hidden="true"><div role="grid"><div role="row">'
        '<span role="gridcell" class="claim-number">ZZ00000000</span></div></div></div>'
        '<table><tr><th>Claim Number</th></tr><tr><td id="real" class="claim-number">'
        "AB12345678</td></tr></table>"
    )
    el = HeuristicRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == _by_id(page, "real")


# -- checklist 4: IdChurn ----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "ident", ["claim-number-8f3a2c19", "ember123", ":r1:", "ctl00_MainContent_claim_number"]
)
def test_a_churned_id_is_not_evidence(ident: str) -> None:
    page = _page(f'<p id="{ident}">AB12345678</p>')
    assert HeuristicRung().resolve(page, CLAIM_NUMBER) is None


@pytest.mark.unit
def test_a_stable_id_names_the_field() -> None:
    page = _page('<p id="claim-number">AB12345678</p>')
    el = HeuristicRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == _by_id(page, "claim-number")
    assert el.anchor.header_text is None


@pytest.mark.unit
def test_a_name_attribute_names_the_field() -> None:
    page = _page('<form><input id="x" name="claim_number" value="AB12345678"></form>')
    el = HeuristicRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == _by_id(page, "x")


@pytest.mark.unit
def test_the_bench_id_churn_still_heals_by_class(bench: BenchPage) -> None:
    page = bench.mutated("IdChurn")
    el = HeuristicRung().resolve(page.source(), CLAIM_NUMBER)
    assert el is not None and el.node_id in page.truth("claim_number")


# -- checklist 6: LabelReword ------------------------------------------------------------------


@pytest.mark.unit
def test_class_rename_and_label_reword_on_one_day_escalate(bench: BenchPage) -> None:
    both = BenchPage(
        apply_all([ClassRename(), LabelReword()], bench.html),
        apply_all([ClassRename(), LabelReword()], bench.marked),
    )
    got = HeuristicRung().attempt(both.source(), CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "no_match"


# -- checklist 7: header verification ----------------------------------------------------------


def _corner_money_table(corner: str) -> str:
    return (
        "<table>"
        f'<tr>{corner}<th colspan="2">Money</th></tr>'
        "<tr><th>Amount</th><th>Prior Amount</th></tr>"
        '<tr><td id="cn">CN</td><td id="am">1.00</td><td>2.00</td></tr>'
        "</table>"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "html",
    [
        _corner_money_table('<td rowspan="2"></td>'),
        _corner_money_table('<td rowspan="2">Claim Number</td>'),
        "<table><thead>"
        '<tr><td rowspan="2"></td><th colspan="2">Money</th></tr>'
        "<tr><th>Amount</th><th>Prior Amount</th></tr>"
        '</thead><tbody><tr><td id="cn">CN</td><td id="am">1.00</td><td>2.00</td></tr>'
        "</tbody></table>",
        "<table>"
        '<tr><td rowspan="2">Claim Number</td><td colspan="2">Money</td></tr>'
        "<tr><th>Amount</th><th>Prior Amount</th></tr>"
        '<tr><td id="cn">CN</td><td id="am">1.00</td><td>2.00</td></tr>'
        "</table>",
    ],
    ids=["P1", "P2", "P3", "P4"],
)
def test_a_rowspanned_header_band_declines(html: str) -> None:
    got = HeuristicRung().attempt(_page(html), AMOUNT)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch"


@pytest.mark.unit
def test_probe_5_never_yields_a_band_cell() -> None:
    page = _page(
        "<table>"
        '<tr><th rowspan="2">Claim Number</th><td colspan="2">Money</td></tr>'
        "<tr><th>Amount</th><th>Prior Amount</th></tr>"
        '<tr><td id="cn">CN</td><td id="am">1.00</td><td>2.00</td></tr>'
        "</table>"
    )
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss)


@pytest.mark.unit
def test_an_attribute_match_in_a_rowspanned_grid_records_no_positional_header() -> None:
    page = _page(
        "<table>"
        '<tr><td rowspan="2"></td><th colspan="2">Money</th></tr>'
        "<tr><th>Prior Amount</th><th>Sum</th></tr>"
        '<tr><td>CN</td><td>1.00</td><td id="am" class="col-amount">2.00</td></tr>'
        "</table>"
    )
    el = HeuristicRung().resolve(page, AMOUNT)
    assert el is not None and el.node_id == _by_id(page, "am")
    assert el.anchor.header_text is None


@pytest.mark.unit
def test_a_plain_header_resolves_and_records_its_text() -> None:
    page = _page(
        "<table><tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn">AB12345678</td><td id="am">100.00</td></tr></table>'
    )
    el = HeuristicRung().resolve(page, AMOUNT)
    assert el is not None and el.node_id == _by_id(page, "am")
    assert el.anchor.header_text == "Amount"


# -- checklist 8: repeated header, spans, tfoot ------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("strategy", ["header", "class"])
def test_a_header_repeated_mid_table_does_not_skip_the_first_record(strategy: str) -> None:
    cls = ' class="col-claim_number"' if strategy == "class" else ""
    head = "<th>Claim Number</th>" if strategy == "header" else "<th>Ref</th>"
    page = _page(
        f"<table><tr>{head}<th>Amount</th></tr>"
        f'<tr><td id="cn"{cls}>AB12345678</td><td>1.00</td></tr>'
        f"<tr>{head}<th>Amount</th></tr>"
        f'<tr><td id="cn2"{cls}>CD87654321</td><td>2.00</td></tr>'
        "</table>"
    )
    el = HeuristicRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == _by_id(page, "cn")


@pytest.mark.unit
def test_a_reordered_repeated_header_never_heals_onto_the_adjacent_column() -> None:
    page = _page(
        "<table><tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn">AB12345678</td><td id="am">1.00</td></tr>'
        "<tr><th>Amount</th><th>Claim Number</th></tr>"
        "<tr><td>2.00</td><td>CD87654321</td></tr></table>"
    )
    for target, ident in ((CLAIM_NUMBER, "cn"), (AMOUNT, "am")):
        el = HeuristicRung().resolve(page, target)
        assert el is not None and el.node_id == _by_id(page, ident)


@pytest.mark.unit
def test_a_colspan_first_data_row_never_lands_on_another_column() -> None:
    page = _page(
        "<table><tr><th>Claimant</th><th>Amount</th><th>Reserve</th></tr>"
        '<tr><td>X</td><td colspan="2">100.00</td></tr></table>'
    )
    got = HeuristicRung().attempt(page, RESERVE)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch"


@pytest.mark.unit
def test_a_class_match_only_in_a_later_record_is_never_taken() -> None:
    """The first record has no cell naming the field: a later record's is another record."""
    page = _page(
        "<table><tr><th>Ref</th><th>Sum</th></tr>"
        "<tr><td>AB12345678</td><td>1.00</td></tr>"
        '<tr><td class="claim-number">CD87654321</td><td>2.00</td></tr></table>'
    )
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "no_match"


def _footed(foot_first: bool, cls: str) -> str:
    foot = (
        f'<tfoot><tr><td id="tot" class="{cls}claim_number">Total</td>'
        f'<td id="sum" class="{cls}amount">301.00</td></tr></tfoot>'
    )
    body = (
        f'<tbody><tr><td id="cn" class="{cls}claim_number">AB12345678</td>'
        f'<td id="am" class="{cls}amount">100.00</td></tr>'
        f'<tr><td class="{cls}claim_number">CD87654321</td>'
        f'<td class="{cls}amount">201.00</td></tr></tbody>'
    )
    return (
        "<table><thead><tr><th>Claim Number</th><th>Amount</th></tr></thead>"
        + (foot + body if foot_first else body + foot)
        + "</table>"
    )


@pytest.mark.unit
@pytest.mark.parametrize("foot_first", [True, False], ids=["tfoot-first", "tfoot-last"])
@pytest.mark.parametrize("cls", ["col-", "zz-"], ids=["class", "header"])
def test_a_tfoot_row_is_never_the_first_record(foot_first: bool, cls: str) -> None:
    page = _page(_footed(foot_first, cls))
    for target, ident in ((CLAIM_NUMBER, "cn"), (AMOUNT, "am")):
        el = HeuristicRung().resolve(page, target)
        assert el is not None and el.node_id == _by_id(page, ident)


# -- checklist 9: ValueSwap --------------------------------------------------------------------


@pytest.mark.unit
def test_value_swap_resolves_the_original_amount_node(bench: BenchPage) -> None:
    """The swap moves TEXT, not markup: the node is right, the number is wrong -- Task 22's
    RowKeyContinuity refuses it, not a rung."""
    page = bench.mutated("ValueSwap")
    el = HeuristicRung().resolve(page.source(), AMOUNT)
    assert el is not None and el.node_id in page.truth("amount")


# -- checklist 10: never a header-band, tfoot or label cell -----------------------------------


@pytest.mark.unit
def test_a_class_on_a_header_cell_is_never_a_value() -> None:
    page = _page('<table><tr><th class="col-amount">Sum</th></tr><tr><td>1.00</td></tr></table>')
    assert HeuristicRung().resolve(page, AMOUNT) is None


@pytest.mark.unit
def test_a_class_on_a_td_header_like_row_is_never_a_value() -> None:
    page = _page('<table><tr><td class="col-amount">Amount</td></tr><tr><td>1.00</td></tr></table>')
    assert HeuristicRung().resolve(page, AMOUNT) is None


@pytest.mark.unit
def test_a_class_on_a_title_row_in_the_header_band_is_never_a_value() -> None:
    page = _page(
        '<table><tr><td colspan="2" class="col-amount">999.00</td></tr>'
        "<tr><th>Ref</th><th>Sum</th></tr>"
        '<tr><td>R1</td><td id="v">1.00</td></tr></table>'
    )
    got = HeuristicRung().attempt(page, AMOUNT)
    assert isinstance(got, Miss) or got.node_id != page.node_id(page.query("td")[0])
    assert HeuristicRung().resolve(page, AMOUNT) is None


@pytest.mark.unit
@pytest.mark.parametrize("tag", ["label", "dt", "legend"])
def test_a_label_element_is_never_a_value(tag: str) -> None:
    page = _page(f'<div><{tag} class="claim-number">Claim No.</{tag}></div>')
    assert HeuristicRung().resolve(page, CLAIM_NUMBER) is None


@pytest.mark.unit
def test_a_dd_beside_a_classed_dt_resolves_to_the_dd() -> None:
    page = _page(
        '<dl><dt class="claim-number">Claim No.</dt><dd id="v" class="claim-number">'
        "AB12345678</dd></dl>"
    )
    el = HeuristicRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == _by_id(page, "v")


@pytest.mark.unit
def test_a_classed_row_is_never_a_value() -> None:
    page = _page(
        '<table><tr><th>Ref</th></tr><tr class="claim-number"><td>AB12345678</td></tr></table>'
    )
    assert HeuristicRung().resolve(page, CLAIM_NUMBER) is None


@pytest.mark.unit
def test_two_loose_matches_are_ambiguous() -> None:
    page = _page('<p class="claim-number">AB12345678</p><p class="claim-number">CD1</p>')
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


@pytest.mark.unit
def test_a_match_in_two_grids_is_ambiguous() -> None:
    table = '<table><tr><th>Ref</th></tr><tr><td class="claim-number">{}</td></tr></table>'
    page = _page(table.format("AB12345678") + table.format("CD87654321"))
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


@pytest.mark.unit
def test_a_nested_match_takes_the_inner_element() -> None:
    page = _page(
        "<table><tr><th>Ref</th></tr>"
        '<tr><td class="col-claim_number"><span id="v" class="claim-number">AB12345678</span>'
        "</td></tr></table>"
    )
    el = HeuristicRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == _by_id(page, "v")


@pytest.mark.unit
def test_an_ambiguous_test_id_is_not_rescued_by_a_class() -> None:
    page = _page(
        '<p data-testid="claim-number">AB12345678</p><p data-qa="claim-number">CD1</p>'
        '<p class="claim-number">EF12345678</p>'
    )
    got = HeuristicRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


# -- header_holds (rung 1's re-verification, Task 17) ------------------------------------------


@pytest.mark.unit
def test_header_holds_on_the_clean_bench_and_fails_after_a_duplicate_label(
    bench: BenchPage,
) -> None:
    clean = bench.source()
    el = HeuristicRung().resolve(clean, CLAIM_NUMBER)
    assert el is not None and el.anchor.header_text == "Claim Number"
    node = clean.node(el.node_id)
    assert node is not None and header_holds(clean, node, "Claim Number")
    assert not header_holds(clean, node, "Amount")

    dup = bench.mutated("DuplicateLabel").source()
    cells = dup.query("td.col-claim_number")
    assert all(not header_holds(dup, c, "Claim Number") for c in cells)


@pytest.mark.unit
def test_header_holds_is_false_outside_a_grid_and_under_a_rowspanned_band() -> None:
    page = _page('<p id="v">AB12345678</p>')
    assert not header_holds(page, page.query("#v")[0], "Claim Number")
    rowspanned = _page(_corner_money_table('<td rowspan="2"></td>'))
    assert not header_holds(rowspanned, rowspanned.query("#am")[0], "Amount")


# -- the bench sweep ---------------------------------------------------------------------------


def _sweep(base: BenchPage, name: str) -> Counter[str]:
    """One mutation class x seeds 0-2 x the six spec fields, judged against the answer key.
    Rung 4 never reads data-sd-*, so resolving on the marked page is resolving on the page."""
    entry = next(e for e in CATALOGUE if e.name == name)
    out: Counter[str] = Counter()
    for seed in range(3):
        page = base.mutated_by(entry.build(random.Random(seed)))
        source = StaticPageSource(page.marked)
        for target in SPEC.targets:
            got = HeuristicRung().attempt(source, target)
            if isinstance(got, Miss):
                out[got.outcome] += 1
                continue
            truth = page.truth(target.name)
            if truth and got.node_id == truth[0]:
                out["correct"] += 1
            else:
                out["other_record" if got.node_id in truth else "WRONG"] += 1
    return out


@pytest.mark.unit
@pytest.mark.parametrize("name", [e.name for e in CATALOGUE])
def test_bench_sweep_rung_four_alone_never_returns_a_wrong_cell(
    name: str, bench: BenchPage
) -> None:
    """Every CATALOGUE mutation x seeds 0-2 x the six spec fields: rung 4 either returns the
    true cell of the FIRST record or declines -- never another column, never another record."""
    counts = _sweep(bench, name)
    assert counts["WRONG"] == 0 and counts["other_record"] == 0, counts
    assert counts["correct"] > 0, counts


@pytest.mark.unit
def test_rung_four_is_registered_after_rung_three() -> None:
    from selector_drift.resolve import cascade

    ids = [r.rung_id for r in cascade.REGISTERED_RUNGS]
    assert 4 in ids and ids == sorted(set(ids))
    assert isinstance(next(r for r in cascade.REGISTERED_RUNGS if r.rung_id == 4), HeuristicRung)
