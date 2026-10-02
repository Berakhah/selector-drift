from __future__ import annotations

import pytest

from selector_drift.resolve.page import StaticPageSource
from selector_drift.resolve.rungs.r2_axtree import AxTreeRung
from selector_drift.resolve.trace import Family, Miss, ResolvedElement
from tests.resolve.conftest import AMOUNT, CLAIM_NUMBER, BenchPage

TABLE = """<html><body>{decoy}<table>
<thead><tr><th>Claim Number</th><th>Amount</th></tr></thead>
<tbody><tr><td id="real">AB12345678</td><td>100.00</td></tr></tbody></table></body></html>"""

DECOYS = [
    '<div aria-hidden="true"><span role="textbox" aria-label="Claim Number">'
    "ZZ00000000</span></div>",
    '<div style="display:none"><input aria-label="Claim Number" value="ZZ00000000"></div>',
    '<input aria-label="Claim Number" value="ZZ00000000" style="position:absolute;left:-9999px">',
    '<input aria-label="Claim Number" value="ZZ00000000" hidden>',
]


@pytest.mark.unit
def test_rung_two_is_text_semantic() -> None:
    assert (AxTreeRung.rung_id, AxTreeRung.family) == (2, Family.TEXT_SEMANTIC)


@pytest.mark.unit
@pytest.mark.parametrize("mutation", ["ClassRename", "DomRestructure", "IdChurn", "TableToDiv"])
def test_axtree_survives_structural_mutations(mutation: str, bench: BenchPage) -> None:
    page = bench.mutated(mutation)
    el = AxTreeRung().resolve(page.source(), CLAIM_NUMBER)
    assert el is not None and el.node_id in page.truth("claim_number")
    assert el.anchor.accessible_name == "Claim Number"


@pytest.mark.unit
def test_axtree_fails_on_label_reword(bench: BenchPage) -> None:
    """Fails -- does not mis-resolve (design 5.1)."""
    got = AxTreeRung().attempt(bench.mutated("LabelReword").source(), CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "no_match"


@pytest.mark.unit
@pytest.mark.parametrize(
    "decoy", DECOYS, ids=["aria-hidden", "display-none-ancestor", "off-screen", "hidden-attribute"]
)
def test_a_hidden_element_is_never_a_candidate(decoy: str) -> None:
    """H9. A hidden node carrying the right accessible name resolves ahead of the real one,
    and every value it yields satisfies the format assertion -- unless visibility gates it."""
    page = StaticPageSource(TABLE.format(decoy=decoy))
    el = AxTreeRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("#real")[0])


@pytest.mark.unit
def test_the_bench_hidden_decoy_never_wins(bench: BenchPage) -> None:
    page = bench.mutated("HiddenDecoy")
    el = AxTreeRung().resolve(page.source(), CLAIM_NUMBER)
    assert el is None or el.node_id in page.truth("claim_number")


@pytest.mark.unit
def test_two_elements_with_the_name_is_ambiguous_not_a_first_match(bench: BenchPage) -> None:
    got = AxTreeRung().attempt(bench.mutated("DuplicateLabel").source(), CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "ambiguous"


@pytest.mark.unit
def test_an_image_header_is_named_by_its_alt_text(bench: BenchPage) -> None:
    page = bench.mutated("ImageHeader")
    el = AxTreeRung().resolve(page.source(), AMOUNT)
    assert el is not None and el.node_id in page.truth("amount")


@pytest.mark.unit
def test_a_header_named_by_aria_labelledby_is_followed_to_its_column() -> None:
    page = StaticPageSource("""<html><body><span id="h" hidden>Claim Number</span><table>
<tr><th aria-labelledby="h">#</th></tr><tr><td id="real">AB12345678</td></tr></table>
</body></html>""")
    el = AxTreeRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("#real")[0])


@pytest.mark.unit
def test_a_labelled_form_control_is_its_own_value() -> None:
    page = StaticPageSource(
        '<html><body><label for="c">Claim Number</label>'
        '<input id="c" value="AB12345678"></body></html>'
    )
    el = AxTreeRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("#c")[0])


@pytest.mark.unit
def test_the_anchor_is_the_name_as_the_page_spells_it() -> None:
    page = StaticPageSource(TABLE.format(decoy="").replace("Claim Number", "CLAIM  NUMBER:"))
    got = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, ResolvedElement) and got.anchor.accessible_name == "CLAIM NUMBER:"
    assert got.anchor.label_text is None and got.anchor.header_text is None


@pytest.mark.unit
def test_the_selector_it_mints_names_exactly_that_element(bench: BenchPage) -> None:
    page = bench.source()
    el = AxTreeRung().resolve(page, CLAIM_NUMBER)
    assert el is not None
    assert [page.node_id(e) for e in page.query(el.selector)] == [el.node_id]


# -- fix round 1 -----------------------------------------------------------------------------


@pytest.mark.unit
def test_a_rowspanned_header_declines_rather_than_heal_the_adjacent_column() -> None:
    """I1. column_of/cell_at count colspan only, so a rowspanned 'Claim Number' header above a
    colspan=2 'Money' group desynchronises column indices: 'Amount' in the second header row
    lands at index 0 in its own row, which is also index 0 of the data row -- Claim Number's
    own cell. Rung 2 must fail closed, never silently heal Amount onto the wrong column."""
    page = StaticPageSource(
        "<html><body><table>"
        '<tr><th rowspan="2">Claim Number</th><th colspan="2">Money</th></tr>'
        "<tr><th>Amount</th><th>Prior Amount</th></tr>"
        '<tr><td id="cn">AB12345678</td><td>100.00</td><td>50.00</td></tr>'
        "</table></body></html>"
    )
    got = AxTreeRung().attempt(page, AMOUNT)
    assert isinstance(got, Miss)
    assert got.outcome == "anchor_mismatch"
    assert "rowspan" in got.detail


@pytest.mark.unit
def test_a_rowspan_free_multi_row_header_still_resolves() -> None:
    """Non-regression: a grouping header row with colspan but no rowspan must not over-decline."""
    page = StaticPageSource(
        "<html><body><table>"
        '<tr><th colspan="2">Money</th></tr>'
        "<tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn">AB12345678</td><td>100.00</td></tr>'
        "</table></body></html>"
    )
    el = AxTreeRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("#cn")[0])
    assert el.anchor.accessible_name == "Claim Number"


@pytest.mark.unit
def test_a_label_cell_is_never_resolved_as_its_own_value() -> None:
    """I2 repro (a). A plain <td> whose content is the label text itself (no header relation,
    no author name) must never be taken as the value it names. With no other element named
    'claim number' anywhere on the page, the exact outcome is a clean no_match."""
    page = StaticPageSource(
        "<html><body><table><tr><td>Claim Number</td><td>AB12345678</td></tr></table></body></html>"
    )
    got = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "no_match"


@pytest.mark.unit
def test_a_td_header_row_never_resolves_to_its_own_header_text() -> None:
    """I2 repro (b). A header row authored with <td> instead of <th> must never be mistaken for
    the value it labels, even though it still carries a columnheader-shaped relationship. No
    td here is a columnheader role, so the exact outcome is a clean no_match."""
    page = StaticPageSource(
        "<html><body><table>"
        "<tr><td>Claim Number</td><td>Amount</td></tr>"
        "<tr><td>AB12345678</td><td>100.00</td></tr>"
        "</table></body></html>"
    )
    got = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "no_match"


@pytest.mark.unit
def test_an_author_named_cell_whose_text_is_a_value_still_resolves() -> None:
    """A cell explicitly labelled by its author (aria-label), as opposed to one merely
    containing the label text, is still allowed to be its own value."""
    page = StaticPageSource(
        '<html><body><table><tr><td aria-label="Claim Number">AB12345678</td>'
        "</tr></table></body></html>"
    )
    el = AxTreeRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("td")[0])


@pytest.mark.unit
@pytest.mark.parametrize(
    "decoy", DECOYS, ids=["aria-hidden", "display-none-ancestor", "off-screen", "hidden-attribute"]
)
def test_a_hidden_only_page_reports_not_visible(decoy: str) -> None:
    """I3. The ax tree prunes aria-hidden/display:none/[hidden] subtrees outright (Task 13), so
    when the only candidate is hidden that way the outcome must still say not_visible, never
    the no_match a genuinely unlabelled page would get."""
    page = StaticPageSource(f"<html><body>{decoy}</body></html>")
    got = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss) and got.outcome == "not_visible"


@pytest.mark.unit
def test_a_hidden_decoy_alongside_a_visible_real_cell_still_resolves() -> None:
    """Non-regression: the not_visible scan must never shadow a real, visible resolution."""
    page = StaticPageSource(TABLE.format(decoy=DECOYS[0]))
    el = AxTreeRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("#real")[0])


# -- fix round 2 -----------------------------------------------------------------------------
#
# header_has_rowspan (round 1) only scanned rows where is_header_row was true, which requires
# every cell in the row to be a columnheader. The commonest real multi-level header has a
# plain, unlabelled corner <td> -- not itself a columnheader -- so that row was "not a header
# row", fell through to first_data_row, and position verification passed against the second
# header row by recomputing the same (wrong) position it was handed. header_band() widens the
# rule to any row carrying a columnheader OR a spanning cell, from the top down.


def _corner_money_table(corner: str) -> str:
    return (
        "<html><body><table>"
        f'<tr>{corner}<th colspan="2">Money</th></tr>'
        "<tr><th>Amount</th><th>Prior Amount</th></tr>"
        '<tr><td id="cn">CN</td><td id="am">1.00</td><td>2.00</td></tr>'
        "</table></body></html>"
    )


@pytest.mark.unit
def test_probe_1_an_unlabelled_rowspanned_corner_never_yields_the_corner_cell() -> None:
    page = StaticPageSource(_corner_money_table('<td rowspan="2"></td>'))
    got = AxTreeRung().attempt(page, AMOUNT)
    assert isinstance(got, Miss)
    assert got.outcome == "anchor_mismatch"
    assert "rowspan" in got.detail


@pytest.mark.unit
def test_probe_2_a_labelled_rowspanned_corner_never_yields_the_corner_cell() -> None:
    page = StaticPageSource(_corner_money_table('<td rowspan="2">Claim Number</td>'))
    got = AxTreeRung().attempt(page, AMOUNT)
    assert isinstance(got, Miss)
    assert got.outcome == "anchor_mismatch"
    assert "rowspan" in got.detail


@pytest.mark.unit
def test_probe_3_the_same_structure_inside_thead_still_declines() -> None:
    page = StaticPageSource(
        "<html><body><table><thead>"
        '<tr><td rowspan="2"></td><th colspan="2">Money</th></tr>'
        "<tr><th>Amount</th><th>Prior Amount</th></tr>"
        "</thead><tbody>"
        '<tr><td id="cn">CN</td><td id="am">1.00</td><td>2.00</td></tr>'
        "</tbody></table></body></html>"
    )
    got = AxTreeRung().attempt(page, AMOUNT)
    assert isinstance(got, Miss)
    assert got.outcome == "anchor_mismatch"
    assert "rowspan" in got.detail


@pytest.mark.unit
def test_probe_4_an_all_td_group_row_never_yields_the_group_cell() -> None:
    """The group row carries no <th> at all -- neither cell is a columnheader -- yet the
    rowspanned corner still has to pull the whole row into the header band."""
    page = StaticPageSource(
        "<html><body><table>"
        '<tr><td rowspan="2">Claim Number</td><td colspan="2">Money</td></tr>'
        "<tr><th>Amount</th><th>Prior Amount</th></tr>"
        '<tr><td id="cn">CN</td><td id="am">1.00</td><td>2.00</td></tr>'
        "</table></body></html>"
    )
    got = AxTreeRung().attempt(page, AMOUNT)
    assert isinstance(got, Miss)
    assert got.outcome == "anchor_mismatch"
    assert "rowspan" in got.detail


@pytest.mark.unit
def test_probe_5_a_mixed_row_headers_right_of_path_never_yields_a_band_cell() -> None:
    """A row mixing a rowheader-shaped <th> with a plain-content <td> ('Money') still pulls
    cell_right_of's candidate into the header band -- that path does not go through
    data_rows(), so it needs its own band check (in_header_band)."""
    page = StaticPageSource(
        "<html><body><table>"
        '<tr><th rowspan="2">Claim Number</th><td colspan="2">Money</td></tr>'
        "<tr><th>Amount</th><th>Prior Amount</th></tr>"
        '<tr><td id="cn">CN</td><td id="am">1.00</td><td>2.00</td></tr>'
        "</table></body></html>"
    )
    got = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss)


@pytest.mark.unit
def test_a_plain_single_row_header_still_resolves() -> None:
    """Non-regression: an ordinary single-row <th> header is unaffected by header_band."""
    page = StaticPageSource(
        "<html><body><table>"
        "<tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn">AB12345678</td><td>100.00</td></tr>'
        "</table></body></html>"
    )
    el = AxTreeRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("#cn")[0])


@pytest.mark.unit
def test_a_row_header_layout_still_resolves() -> None:
    """Non-regression: a data row whose first cell is a rowheader beside plain <td>s, with no
    spans, must not be swept into the header band."""
    page = StaticPageSource(
        '<html><body><table><tr><th scope="row">Claim Number</th>'
        "<td>AB12345678</td></tr></table></body></html>"
    )
    el = AxTreeRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("td")[0])


# -- fix round 3 -----------------------------------------------------------------------------
#
# Round 2's header_band keyed membership on spans as well as columnheader role, so it swept in
# rows that were never header: a second data row with a merged cell (B1), or a grouped
# line-item's rowspanned first cell (B2). Membership is now keyed on columnheader alone -- the
# band is every row from the top through the LAST columnheader row, inclusive; a row above that
# line is in the band by position even with no columnheader of its own (keeps P4's grouping row
# in); a row below it is never swept in by a span on its own cells (fixes B1 and B2); and with
# no columnheader anywhere the band is empty.


@pytest.mark.unit
def test_b1_a_merged_cell_in_the_second_data_row_does_not_pull_it_into_the_band() -> None:
    """B1. round 2's span-keyed band pulled row 2 in (its colspan=2 cell), so first_data_row
    became row 3 and CLAIM_NUMBER silently resolved to a different record (#cn2) -- a wrong
    record, not even a wrong column, with nothing in the trace to say so."""
    page = StaticPageSource(
        "<html><body><table>"
        "<tr><th>Claim Number</th><th>Amount</th><th>Prior</th></tr>"
        '<tr><td id="cn">AB12345678</td><td colspan="2">100.00</td></tr>'
        '<tr><td id="cn2">CD87654321</td><td>1.00</td><td>2.00</td></tr>'
        "</table></body></html>"
    )
    el = AxTreeRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("#cn")[0])


@pytest.mark.unit
def test_b2_a_rowspanned_first_data_row_is_not_swept_into_the_band() -> None:
    """B2. round 2's span-keyed band pulled the first (and only) data row in because its own
    cell has rowspan=2, tripping header_has_rowspan and declining a perfectly good resolution
    -- a regression against round 1, which resolved this. Grouped line-item tables (one cell
    spanning several line items) are a common, legitimate data-row shape."""
    page = StaticPageSource(
        "<html><body><table>"
        "<tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn" rowspan="2">AB12345678</td><td id="am">100.00</td></tr>'
        "<tr><td>200.00</td></tr>"
        "</table></body></html>"
    )
    got_amount = AxTreeRung().attempt(page, AMOUNT)
    assert isinstance(got_amount, ResolvedElement)
    assert got_amount.node_id == page.node_id(page.query("#am")[0])
    got_claim = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got_claim, ResolvedElement)
    assert got_claim.node_id == page.node_id(page.query("#cn")[0])


@pytest.mark.unit
def test_a_row_header_whose_value_cell_spans_still_resolves() -> None:
    """A detail row with no columnheader anywhere on the page: the band is empty regardless of
    spans, so a rowheader's value cell spanning multiple columns (a full-width detail value) is
    not swept into a band that doesn't exist, and cell_right_of's relationship still holds --
    this is the brief's rung-2 rowheader relationship working as intended, not a special case."""
    page = StaticPageSource(
        '<html><body><table><tr><th scope="row">Claim Number</th>'
        '<td colspan="3">AB12345678</td></tr></table></body></html>'
    )
    el = AxTreeRung().resolve(page, CLAIM_NUMBER)
    assert el is not None and el.node_id == page.node_id(page.query("td")[0])


# -- fix round 4 -----------------------------------------------------------------------------
#
# Round 3's band ran from the top through the LAST columnheader row, so a header row repeated
# mid-table swept every data row above it into the band and CLAIM_NUMBER silently resolved to
# a different record. The band now ends at the end of the FIRST contiguous run of columnheader
# rows; below it, a row carrying a columnheader (a repeated header, a <tbody> section label) or
# one in which every cell spans more than one column (a group / sub-header / divider row) is
# excluded, never data.


@pytest.mark.unit
def test_a_header_row_repeated_mid_table_does_not_skip_the_first_record() -> None:
    page = StaticPageSource(
        "<html><body><table>"
        "<tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn">AB12345678</td><td>1.00</td></tr>'
        "<tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn2">CD87654321</td><td>2.00</td></tr>'
        "</table></body></html>"
    )
    got = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, ResolvedElement)
    assert got.node_id == page.node_id(page.query("#cn")[0])


@pytest.mark.unit
def test_a_spanning_group_row_below_the_headers_is_never_data() -> None:
    page = StaticPageSource(
        "<html><body><table>"
        "<tr><th>Claim Number</th><th>Amount</th><th>B</th><th>C</th></tr>"
        '<tr><td colspan="2">Group X</td><td colspan="2">Group Y</td></tr>'
        '<tr><td id="cn">AB12345678</td><td>1.00</td><td>b</td><td>c</td></tr>'
        "</table></body></html>"
    )
    got = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, ResolvedElement)
    assert got.node_id == page.node_id(page.query("#cn")[0])


@pytest.mark.unit
def test_a_title_row_above_the_headers_stays_in_the_band() -> None:
    page = StaticPageSource(
        "<html><body><table>"
        '<tr><td colspan="2">Open claims</td></tr>'
        "<tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn">AB12345678</td><td id="am">1.00</td></tr>'
        "</table></body></html>"
    )
    for target, ident in ((CLAIM_NUMBER, "#cn"), (AMOUNT, "#am")):
        got = AxTreeRung().attempt(page, target)
        assert isinstance(got, ResolvedElement)
        assert got.node_id == page.node_id(page.query(ident)[0])


@pytest.mark.unit
def test_a_tbody_section_label_row_is_never_the_value() -> None:
    """A <th colspan> section label opening the <tbody>: never resolved to. It joins the first
    header run, so column_header() verifies against it and the rung declines (no_match)."""
    page = StaticPageSource(
        "<html><body><table>"
        "<thead><tr><th>Claim Number</th><th>Amount</th></tr></thead>"
        '<tbody><tr><th colspan="2">Open claims</th></tr>'
        '<tr><td id="cn">AB12345678</td><td>1.00</td></tr></tbody>'
        "</table></body></html>"
    )
    for target in (CLAIM_NUMBER, AMOUNT):
        got = AxTreeRung().attempt(page, target)
        assert isinstance(got, Miss)
        assert got.outcome == "no_match"


@pytest.mark.unit
def test_a_reordered_repeated_header_never_heals_onto_the_adjacent_column() -> None:
    """A repeated header row with its columns in a different order sits outside the band, so
    it may not serve as an anchor: verifying against it lined CLAIM_NUMBER up with the Amount
    cell of the first record -- the adjacent-column failure. Verification uses band headers."""
    page = StaticPageSource(
        "<html><body><table>"
        "<tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn">AB12345678</td><td id="am">1.00</td></tr>'
        "<tr><th>Amount</th><th>Claim Number</th></tr>"
        "<tr><td>2.00</td><td>CD87654321</td></tr>"
        "</table></body></html>"
    )
    for target, ident in ((CLAIM_NUMBER, "#cn"), (AMOUNT, "#am")):
        got = AxTreeRung().attempt(page, target)
        assert isinstance(got, ResolvedElement)
        assert got.node_id == page.node_id(page.query(ident)[0])


@pytest.mark.unit
def test_a_grouping_row_rowspanned_into_a_later_header_row_is_band_not_data() -> None:
    """A grouping row between two columnheader rows whose corner rowspans down into the second
    one is header structure, not a record: taking it as data resolved CLAIM_NUMBER to the
    corner cell's text. It belongs to the band, whose rowspan then declines (rule 3)."""
    page = StaticPageSource(
        "<html><body><table>"
        '<tr><th>Claim Number</th><th colspan="2">Money</th></tr>'
        '<tr><td rowspan="2">corner</td><td>g1</td><td>g2</td></tr>'
        "<tr><th>Amount</th><th>Prior Amount</th></tr>"
        '<tr><td id="cn">AB12345678</td><td id="am">1.00</td><td>2.00</td></tr>'
        "</table></body></html>"
    )
    got = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss)
    assert got.outcome == "anchor_mismatch"
    assert "rowspan" in got.detail


@pytest.mark.unit
def test_a_rowspanned_data_row_above_a_repeated_header_is_still_data() -> None:
    """Non-regression for the rule above: a grouped line item whose rowspan ends before the
    repeated header is an ordinary record (B2's shape) and still resolves."""
    page = StaticPageSource(
        "<html><body><table>"
        "<tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn" rowspan="2">AB12345678</td><td id="am">100.00</td></tr>'
        "<tr><td>200.00</td></tr>"
        "<tr><th>Claim Number</th><th>Amount</th></tr>"
        '<tr><td id="cn2">CD87654321</td><td>2.00</td></tr>'
        "</table></body></html>"
    )
    got = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, ResolvedElement)
    assert got.node_id == page.node_id(page.query("#cn")[0])


# -- fix round 5 -----------------------------------------------------------------------------
#
# Round 4 applied the data-row exclusions to a directly author-named element too, so a labelled
# control in a full-width (all-cells-span) row stopped resolving. The exclusions apply only to
# cell-derived values (cell_under, cell_right_of); a directly named element gets only the
# header-band check. And a <tfoot> row is never data, whatever its source order.


@pytest.mark.unit
@pytest.mark.parametrize(
    "where",
    [
        '<table><tr><td>Name</td><td><input aria-label="Name"></td></tr>'
        '<tr><td colspan="2">{input}</td></tr></table>',
        "<table><tr><td>Name</td><td>{input}</td></tr></table>",
        "<div>{input}</div>",
    ],
    ids=["full-width-row", "plain-cell", "outside-a-table"],
)
def test_a_directly_named_control_resolves_wherever_its_row_sits(where: str) -> None:
    control = '<input id="cn" aria-label="Claim Number" value="AB12345678">'
    page = StaticPageSource("<html><body>" + where.format(input=control) + "</body></html>")
    got = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, ResolvedElement)
    assert got.node_id == page.node_id(page.query("#cn")[0])


@pytest.mark.unit
def test_a_directly_named_control_inside_the_header_band_never_resolves() -> None:
    page = StaticPageSource(
        "<html><body><table>"
        '<tr><td colspan="2"><input aria-label="Claim Number" value="AB12345678"></td></tr>'
        "<tr><th>Ref</th><th>Amount</th></tr>"
        "<tr><td>x</td><td>1.00</td></tr>"
        "</table></body></html>"
    )
    got = AxTreeRung().attempt(page, CLAIM_NUMBER)
    assert isinstance(got, Miss)


def _footed_table(foot_first: bool) -> str:
    foot = '<tfoot><tr><td id="tot">Total</td><td id="sum">301.00</td></tr></tfoot>'
    body = (
        '<tbody><tr><td id="cn">AB12345678</td><td id="am">100.00</td></tr>'
        "<tr><td>CD87654321</td><td>201.00</td></tr></tbody>"
    )
    return (
        "<html><body><table><thead><tr><th>Claim Number</th><th>Amount</th></tr></thead>"
        + (foot + body if foot_first else body + foot)
        + "</table></body></html>"
    )


@pytest.mark.unit
@pytest.mark.parametrize("foot_first", [True, False], ids=["tfoot-first", "tfoot-last"])
def test_a_tfoot_row_is_never_the_first_record(foot_first: bool) -> None:
    page = StaticPageSource(_footed_table(foot_first))
    for target, ident in ((CLAIM_NUMBER, "#cn"), (AMOUNT, "#am")):
        got = AxTreeRung().attempt(page, target)
        assert isinstance(got, ResolvedElement)
        assert got.node_id == page.node_id(page.query(ident)[0])


@pytest.mark.unit
def test_a_tfoot_row_header_never_yields_a_footer_cell() -> None:
    """cell_right_of from a rowheader inside <tfoot> is cell-derived: never a value."""
    page = StaticPageSource(
        "<html><body><table><tbody>"
        '<tr><th scope="row">Reference</th><td>R-1</td></tr></tbody>'
        '<tfoot><tr><th scope="row">Amount</th><td>301.00</td></tr></tfoot>'
        "</table></body></html>"
    )
    got = AxTreeRung().attempt(page, AMOUNT)
    assert isinstance(got, Miss)
