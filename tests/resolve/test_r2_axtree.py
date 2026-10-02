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
