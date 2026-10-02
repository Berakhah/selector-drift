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
