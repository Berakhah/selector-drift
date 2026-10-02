from __future__ import annotations

import random
from collections import Counter

import pytest

from selector_drift.resolve.page import StaticPageSource
from selector_drift.resolve.rungs.r3_anchors import AnchorRung, locate_by_label, value_for
from selector_drift.resolve.trace import Family, Miss, ResolvedElement
from selector_drift.spec.schema import Relation
from testbench.mutations.catalogue import CATALOGUE
from tests.resolve.conftest import AMOUNT, CLAIM_NUMBER, SPEC, BenchPage

DETAIL = """<html><body><h1>Claim AB12345678</h1><dl class="claim-detail">
<dt>Claim Number</dt><dd id="value">AB12345678</dd>
<dt>Claimant</dt><dd>Test Person One</dd></dl></body></html>"""

RIGHT_OF = CLAIM_NUMBER.model_copy(update={"relation": Relation.RIGHT_OF})
AMOUNT_RIGHT_OF = AMOUNT.model_copy(update={"relation": Relation.RIGHT_OF})


def _page(body: str) -> StaticPageSource:
    return StaticPageSource(f"<html><body>{body}</body></html>")


def _table(rows: str) -> StaticPageSource:
    return _page(f"<table>{rows}</table>")


def _is(page: StaticPageSource, got: ResolvedElement | Miss, ident: str) -> bool:
    return isinstance(got, ResolvedElement) and got.node_id == page.node_id(page.query(ident)[0])


@pytest.mark.unit
def test_rung_three_is_text_semantic() -> None:
    assert (AnchorRung.rung_id, AnchorRung.family) == (3, Family.TEXT_SEMANTIC)


@pytest.mark.unit
@pytest.mark.parametrize("mutation", ["ClassRename", "IdChurn", "FrameworkMarkup", "TableToDiv"])
def test_anchors_survive_restyling_and_markup_swaps(mutation: str, bench: BenchPage) -> None:
    page = bench.mutated(mutation)
    el = AnchorRung().resolve(page.source(), CLAIM_NUMBER)
    assert el is not None and el.node_id in page.truth("claim_number")
    assert el.anchor.label_text == "Claim Number"


@pytest.mark.unit
def test_anchors_fail_on_label_reword(bench: BenchPage) -> None:
    got = AnchorRung().attempt(bench.mutated("LabelReword").source(), CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "no_match"


@pytest.mark.unit
def test_a_duplicated_label_is_refused_not_taken_first(bench: BenchPage) -> None:
    """Task 11A: rung 3 must refuse an ambiguous anchor rather than take the first."""
    got = AnchorRung().attempt(bench.mutated("DuplicateLabel").source(), CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


@pytest.mark.unit
def test_a_column_header_relates_to_its_column_whatever_the_spec_relation(bench: BenchPage) -> None:
    """Spec v1 said right_of (it described the detail page); applied literally to a
    results-table header it would land on the next header. Spec v2 dropped it (Task 12 Step 0),
    so the relation is set here to keep proving the markup wins."""
    el = AnchorRung().resolve(bench.source(), RIGHT_OF)
    assert el is not None and el.node_id == bench.truth("claim_number")[0]


@pytest.mark.unit
def test_a_definition_term_relates_to_its_definition() -> None:
    page = StaticPageSource(DETAIL)
    el = AnchorRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("#value")[0])


@pytest.mark.unit
def test_a_label_element_relates_to_its_control() -> None:
    page = _page('<label for="c">Claim Number:</label><input id="c" value="AB12345678">')
    el = AnchorRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("#c")[0])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("relation", "body"),
    [
        (Relation.RIGHT_OF, '<div><span>Claim Number</span><span id="v">AB12345678</span></div>'),
        (Relation.BELOW, '<div><p>Claim Number</p></div><div><p id="v">AB12345678</p></div>'),
        (
            Relation.WITHIN,
            '<section><h2>Claim Number</h2><div><b id="v">AB12345678</b></div></section>',
        ),
    ],
)
def test_a_free_text_anchor_follows_the_declared_relation(relation: Relation, body: str) -> None:
    page = _page(body)
    target = CLAIM_NUMBER.model_copy(update={"relation": relation})
    el = AnchorRung().resolve(page, target)
    assert el is not None and el.node_id == page.node_id(page.query("#v")[0])


@pytest.mark.unit
def test_a_free_text_anchor_with_no_declared_relation_resolves_nothing() -> None:
    page = _page("<div><span>Claim Number</span><span>AB12345678</span></div>")
    target = CLAIM_NUMBER.model_copy(update={"relation": None})
    assert AnchorRung().resolve(page, target) is None


@pytest.mark.unit
def test_a_hidden_label_is_never_an_anchor() -> None:
    page = _page("<div hidden><span>Claim Number</span><span>ZZ00000000</span></div>")
    got = AnchorRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "not_visible"


@pytest.mark.unit
def test_label_text_is_the_label_as_the_page_spells_it() -> None:
    page = StaticPageSource(DETAIL.replace("<dt>Claim Number</dt>", "<dt>Claim  number:</dt>"))
    el = AnchorRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.anchor.label_text == "Claim number:"
    assert el.anchor.accessible_name is None and el.anchor.header_text is None


# -- adversarial checklist (Task 15 dispatch) -------------------------------------------------
# 1. the decoy column and a reworded label


@pytest.mark.unit
def test_1_the_prior_amount_decoy_never_anchors_amount(bench: BenchPage) -> None:
    page = bench.mutated("DecoyColumn")
    el = AnchorRung().resolve(page.source(), AMOUNT)
    assert el is not None and el.node_id == page.truth("amount")[0]


@pytest.mark.unit
@pytest.mark.parametrize(
    "header",
    ["<th>Prior <span>Amount</span></th>", "<th><span>Prior</span> <span>Amount</span></th>"],
    ids=["text-then-span", "two-spans"],
)
def test_1_a_label_word_inside_a_longer_header_is_not_an_anchor(header: str) -> None:
    """'Amount' spelled by a span inside the 'Prior Amount' header must never take that
    header's column: the markup container has to spell the label itself."""
    page = _table(
        f"<tr><th>Claim Number</th>{header}</tr><tr><td>AB12345678</td><td>9.00</td></tr>"
    )
    for target in (AMOUNT, AMOUNT_RIGHT_OF):
        got = AnchorRung().attempt(page, target)
        assert isinstance(got, Miss) and got.outcome == "no_match"


@pytest.mark.unit
def test_1_a_label_word_inside_a_free_text_phrase_is_not_an_anchor() -> None:
    page = _page('<div>Prior <b>Amount</b> <span id="v">9.00</span></div>')
    got = AnchorRung().attempt(page, AMOUNT_RIGHT_OF)
    assert isinstance(got, Miss) and got.outcome == "no_match"


# 3. hidden labels


HIDDEN = [
    "<div hidden><span>Claim Number</span><span>ZZ00000000</span></div>",
    '<div aria-hidden="true"><span>Claim Number</span><span>ZZ00000000</span></div>',
    '<div style="display:none"><span>Claim Number</span><span>ZZ00000000</span></div>',
    '<div style="position:absolute;left:-9999px"><span>Claim Number</span>'
    "<span>ZZ00000000</span></div>",
]
HIDDEN_IDS = ["hidden-attr", "aria-hidden", "display-none", "off-screen"]


@pytest.mark.unit
@pytest.mark.parametrize("decoy", HIDDEN, ids=HIDDEN_IDS)
def test_3_a_hidden_only_label_reports_not_visible(decoy: str) -> None:
    got = AnchorRung().attempt(_page(decoy), RIGHT_OF)
    assert isinstance(got, Miss) and got.outcome == "not_visible"


@pytest.mark.unit
@pytest.mark.parametrize("decoy", HIDDEN, ids=HIDDEN_IDS)
def test_3_a_hidden_decoy_beside_a_visible_label_yields_the_real_value(decoy: str) -> None:
    page = _page(decoy + '<div><span>Claim Number</span><span id="v">AB12345678</span></div>')
    assert _is(page, AnchorRung().attempt(page, RIGHT_OF), "#v")


@pytest.mark.unit
def test_3_a_visible_label_whose_value_is_hidden_reports_not_visible() -> None:
    page = _page("<div><span>Claim Number</span><span hidden>ZZ00000000</span></div>")
    got = AnchorRung().attempt(page, RIGHT_OF)
    assert isinstance(got, Miss) and got.outcome == "not_visible"


@pytest.mark.unit
@pytest.mark.parametrize("variant", ["aria-hidden", "display-none"])
def test_3_the_bench_hidden_decoy_never_wins(variant: str, bench: BenchPage) -> None:
    from testbench.mutations.adversarial.hidden_decoy import HiddenDecoy

    page = bench.mutated_by(HiddenDecoy(variant=variant))
    el = AnchorRung().resolve(page.source(), CLAIM_NUMBER)
    assert el is not None and el.node_id == page.truth("claim_number")[0]


# 4. an image header is rung 2's case


@pytest.mark.unit
def test_4_an_image_header_is_no_match_at_rung_three(bench: BenchPage) -> None:
    got = AnchorRung().attempt(bench.mutated("ImageHeader").source(), AMOUNT)
    assert isinstance(got, Miss) and got.outcome == "no_match"


# 5. markup wins over the spec relation


@pytest.mark.unit
def test_5_right_of_on_a_column_header_never_lands_on_the_next_header() -> None:
    page = _table(
        "<tr><th>Claim Number</th><th>Claimant</th></tr>"
        '<tr><td id="cn">AB12345678</td><td>Test Person</td></tr>'
    )
    assert _is(page, AnchorRung().attempt(page, RIGHT_OF), "#cn")


@pytest.mark.unit
def test_5_a_free_text_right_of_never_yields_a_header() -> None:
    """A free-text label beside a header cell: the 'value' would be a header -- refused."""
    page = _table(
        "<tr><td>Claim Number</td><th>Claimant</th></tr><tr><td>AB12345678</td><td>X</td></tr>"
    )
    got = AnchorRung().attempt(page, RIGHT_OF)
    assert isinstance(got, Miss)


# 6. multi-level / rowspan headers (Task 14 probes P1-P5)


def _corner_money_table(corner: str, *, thead: bool = False) -> StaticPageSource:
    head = (
        f'<tr>{corner}<th colspan="2">Money</th></tr><tr><th>Amount</th><th>Prior Amount</th></tr>'
    )
    body = '<tr><td id="cn">CN</td><td id="am">1.00</td><td>2.00</td></tr>'
    if thead:
        return _table(f"<thead>{head}</thead><tbody>{body}</tbody>")
    return _table(head + body)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("corner", "thead"),
    [
        ('<td rowspan="2"></td>', False),
        ('<td rowspan="2">Claim Number</td>', False),
        ('<td rowspan="2"></td>', True),
    ],
    ids=["P1-unlabelled-corner", "P2-labelled-corner", "P3-in-thead"],
)
def test_6_a_rowspanned_header_band_declines_amount(corner: str, thead: bool) -> None:
    got = AnchorRung().attempt(_corner_money_table(corner, thead=thead), AMOUNT)
    assert isinstance(got, Miss) and got.outcome == "anchor_mismatch"
    assert "rowspan" in got.detail


@pytest.mark.unit
@pytest.mark.parametrize("target", [CLAIM_NUMBER, RIGHT_OF], ids=["no-relation", "right_of"])
@pytest.mark.parametrize(
    "corner",
    [
        '<td rowspan="2">Claim Number</td><td colspan="2">Money</td>',  # P4
        '<th rowspan="2">Claim Number</th><td colspan="2">Money</td>',  # P5
        '<td rowspan="2">Claim Number</td><th colspan="2">Money</th>',  # P2
    ],
    ids=["P4-all-td-group-row", "P5-mixed-row", "P2-labelled-corner"],
)
def test_6_a_corner_label_never_yields_a_band_or_adjacent_cell(corner: str, target: object) -> None:
    page = _table(
        f"<tr>{corner}</tr><tr><th>Amount</th><th>Prior Amount</th></tr>"
        '<tr><td id="cn">CN</td><td id="am">1.00</td><td>2.00</td></tr>'
    )
    got = AnchorRung().attempt(page, target)  # type: ignore[arg-type]
    assert isinstance(got, Miss)


@pytest.mark.unit
def test_6_a_rowspan_free_multi_row_header_still_resolves() -> None:
    page = _table(
        '<tr><th colspan="2">Money</th></tr><tr><th>Claim Number</th><th>Amount</th></tr>'
        '<tr><td id="cn">AB12345678</td><td id="am">100.00</td></tr>'
    )
    assert _is(page, AnchorRung().attempt(page, CLAIM_NUMBER), "#cn")
    assert _is(page, AnchorRung().attempt(page, AMOUNT), "#am")


# 7. repeated headers, spans in data rows, tfoot


@pytest.mark.unit
@pytest.mark.parametrize(
    "second_header",
    [
        "<tr><th>Claim Number</th><th>Amount</th></tr>",
        "<tr><th>Amount</th><th>Claim Number</th></tr>",
    ],
    ids=["same-order", "reordered"],
)
def test_7_a_header_repeated_mid_table_never_moves_the_record(second_header: str) -> None:
    page = _table(
        "<tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn">AB12345678</td><td id="am">1.00</td></tr>'
        + second_header
        + "<tr><td>CD87654321</td><td>2.00</td></tr>"
    )
    assert _is(page, AnchorRung().attempt(page, CLAIM_NUMBER), "#cn")
    assert _is(page, AnchorRung().attempt(page, AMOUNT), "#am")


@pytest.mark.unit
def test_7_a_colspan_in_the_first_data_row_keeps_the_first_record() -> None:
    page = _table(
        "<tr><th>Claim Number</th><th>Amount</th><th>Prior</th></tr>"
        '<tr><td id="cn">AB12345678</td><td colspan="2">100.00</td></tr>'
        "<tr><td>CD87654321</td><td>1.00</td><td>2.00</td></tr>"
    )
    assert _is(page, AnchorRung().attempt(page, CLAIM_NUMBER), "#cn")


@pytest.mark.unit
def test_7_a_rowspan_in_the_first_data_row_keeps_the_first_record() -> None:
    page = _table(
        "<tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn" rowspan="2">AB12345678</td><td id="am">100.00</td></tr>'
        "<tr><td>200.00</td></tr>"
    )
    assert _is(page, AnchorRung().attempt(page, CLAIM_NUMBER), "#cn")
    assert _is(page, AnchorRung().attempt(page, AMOUNT), "#am")


@pytest.mark.unit
@pytest.mark.parametrize("foot_first", [True, False], ids=["tfoot-first", "tfoot-last"])
def test_7_a_tfoot_row_is_never_the_record(foot_first: bool) -> None:
    foot = '<tfoot><tr><td>Total</td><td id="sum">301.00</td></tr></tfoot>'
    body = (
        '<tbody><tr><td id="cn">AB12345678</td><td id="am">100.00</td></tr>'
        "<tr><td>CD87654321</td><td>201.00</td></tr></tbody>"
    )
    page = _table(
        "<thead><tr><th>Claim Number</th><th>Amount</th></tr></thead>"
        + (foot + body if foot_first else body + foot)
    )
    assert _is(page, AnchorRung().attempt(page, CLAIM_NUMBER), "#cn")
    assert _is(page, AnchorRung().attempt(page, AMOUNT), "#am")


@pytest.mark.unit
def test_7_a_free_text_label_in_a_tfoot_row_never_yields_the_footer_cell() -> None:
    page = _table(
        "<tbody><tr><td>Reference</td><td>R-1</td></tr></tbody>"
        "<tfoot><tr><td>Amount</td><td>301.00</td></tr></tfoot>"
    )
    got = AnchorRung().attempt(page, AMOUNT_RIGHT_OF)
    assert isinstance(got, Miss)


@pytest.mark.unit
def test_7_a_spanning_group_row_label_never_yields_a_cell() -> None:
    page = _table(
        "<tr><th>Ref</th><th>B</th><th>C</th><th>D</th></tr>"
        '<tr><td colspan="2">Claim Number</td><td colspan="2">AB12345678</td></tr>'
        "<tr><td>x</td><td>y</td><td>z</td><td>w</td></tr>"
    )
    got = AnchorRung().attempt(page, RIGHT_OF)
    assert isinstance(got, Miss)


# 8. a label cell never resolves to itself


@pytest.mark.unit
def test_8_a_label_cell_with_right_of_resolves_the_value_cell() -> None:
    page = _table('<tr><td>Claim Number</td><td id="v">AB12345678</td></tr>')
    assert _is(page, AnchorRung().attempt(page, RIGHT_OF), "#v")


@pytest.mark.unit
def test_8_a_label_cell_without_a_relation_resolves_nothing_never_itself() -> None:
    page = _table('<tr><td>Claim Number</td><td id="v">AB12345678</td></tr>')
    got = AnchorRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "no_match"


@pytest.mark.unit
def test_8_a_label_cell_whose_right_neighbour_is_empty_never_skips_to_the_next_column() -> None:
    page = _table('<tr><td>Claim Number</td><td id="e"></td><td id="n">Test Person</td></tr>')
    got = AnchorRung().attempt(page, RIGHT_OF)
    assert isinstance(got, Miss) or _is(page, got, "#e")
    assert not _is(page, got, "#n")


@pytest.mark.unit
def test_8_a_value_spelling_the_label_is_never_taken() -> None:
    page = _page("<div><span>Claim Number</span><span>Claim Number</span></div>")
    got = AnchorRung().attempt(page, RIGHT_OF)
    assert isinstance(got, Miss)


# 9. markup relations and free-text relations


@pytest.mark.unit
def test_9_a_row_header_relates_to_its_row() -> None:
    page = _table('<tr><th scope="row">Claim Number</th><td id="v">AB12345678</td></tr>')
    assert _is(page, AnchorRung().attempt(page, CLAIM_NUMBER), "#v")


@pytest.mark.unit
def test_9_a_wrapping_label_relates_to_its_control() -> None:
    page = _page('<label><span>Claim Number</span> <input id="c" value="AB12345678"></label>')
    assert _is(page, AnchorRung().attempt(page, CLAIM_NUMBER), "#c")


@pytest.mark.unit
def test_9_a_label_for_a_non_control_relates_to_nothing() -> None:
    page = _page('<label for="d">Claim Number</label><div id="d">AB12345678</div>')
    got = AnchorRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "no_match"


@pytest.mark.unit
def test_9_a_dt_without_its_own_dd_relates_to_nothing() -> None:
    page = _page("<dl><dt>Claim Number</dt><dt>Claimant</dt><dd>Test Person</dd></dl>")
    got = AnchorRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss)


@pytest.mark.unit
def test_9_within_declines_when_the_container_holds_more_than_one_value() -> None:
    page = _page(
        "<section><h2>Claim Number</h2><div><b>AB12345678</b></div><p>CD87654321</p></section>"
    )
    target = CLAIM_NUMBER.model_copy(update={"relation": Relation.WITHIN})
    assert isinstance(AnchorRung().attempt(page, target), Miss)


@pytest.mark.unit
def test_9_below_inside_a_grid_declines_rather_than_read_the_next_cell() -> None:
    page = _table(
        "<tr><td>Claim Number</td><td>Amount</td></tr><tr><td>AB12345678</td><td>1.00</td></tr>"
    )
    target = CLAIM_NUMBER.model_copy(update={"relation": Relation.BELOW})
    assert isinstance(AnchorRung().attempt(page, target), Miss)


@pytest.mark.unit
def test_value_for_and_locate_by_label_are_the_public_surface() -> None:
    page = StaticPageSource(DETAIL)
    dt = page.query("dt")[0]
    assert value_for(page, dt, None) is page.query("#value")[0]
    found = locate_by_label(page, "Claim Number", None)
    assert not isinstance(found, Miss) and found[0] is page.query("#value")[0]
    empty = locate_by_label(page, "  ", None)
    assert isinstance(empty, Miss) and empty.outcome == "no_match"


# -- the bench sweep ---------------------------------------------------------------------------


def sweep(bench: BenchPage) -> Counter[str]:
    """Every CATALOGUE mutation x seeds 0-2 x the six spec fields. 'wrong' is a resolution to
    a cell that is no field's truth cell; 'other_record' is the right field in a later row."""
    out: Counter[str] = Counter()
    for entry in CATALOGUE:
        for seed in range(3):
            page = bench.mutated_by(entry.build(random.Random(seed)))
            source = page.source()
            for target in SPEC.targets:
                got = AnchorRung().attempt(source, target)
                if isinstance(got, Miss):
                    out[got.outcome] += 1
                    continue
                truth = page.truth(target.name)
                if truth and got.node_id == truth[0]:
                    out["correct"] += 1
                elif got.node_id in truth:
                    out[f"other_record:{entry.name}:{target.name}"] += 1
                else:
                    out[f"wrong:{entry.name}:{target.name}"] += 1
    return out


@pytest.mark.unit
def test_the_bench_sweep_never_returns_a_wrong_cell(bench: BenchPage) -> None:
    got = sweep(bench)
    bad = {k: v for k, v in got.items() if k.startswith(("wrong", "other_record"))}
    assert bad == {}, got
    assert got["correct"] > 0
