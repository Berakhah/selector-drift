# ruff: noqa: E501  (HTML fixtures are kept on one line per row)
"""read() on the testbench's own mutated markup, and on repeated layout wrappers.

The misalignment cases must be refused wherever the table sits: a repeated layout <div>
above the table must never turn two unrelated cells into one plausible row."""

from __future__ import annotations

import pytest

from selector_drift.extract.reader import RowMisaligned, read
from selector_drift.resolve.trace import ResolvedElement
from testbench.mutations.dom_restructure import DomRestructure
from testbench.mutations.table_to_div import TableToDiv
from tests.extract.conftest import AT, ONE_FIELD_SPEC, SPEC, TRACES, _el

BASE = """<html><body><header>h</header><main id="main"><h1>Claims</h1>
<table class="results" id="results"><caption>c</caption>
<thead><tr><th>Claim Number</th><th>Amount</th></tr></thead>
<tbody>
<tr><td class="col-claim_number"><a href="/c/1">AB12345678</a></td><td class="col-amount">1,200.00</td></tr>
<tr><td class="col-claim_number"><a href="/c/2">CD87654321</a></td><td class="col-amount">90.50</td></tr>
<tr><td class="col-claim_number"><a href="/c/3">EF11112222</a></td><td class="col-amount">7.00</td></tr>
</tbody></table><nav><ul><li>1</li><li>2</li></ul></nav></main><footer>f</footer></body></html>"""
D = TableToDiv().apply(BASE)
R = DomRestructure(inner_tag="div").apply(D)
CLAIMS = ["AB12345678", "CD87654321", "EF11112222"]


def cell(r: int, c: int) -> str:
    return f"//*[@role='rowgroup'][2]/*[@role='row'][{r}]/*[@role='cell'][{c}]"


def _read(html: str, res: dict[str, ResolvedElement], spec=SPEC):  # type: ignore[no-untyped-def]
    return read(html, res, spec, run_key="k", snapshot_id="s" * 64, traces=TRACES, observed_at=AT)


def _pair(html: str, a: str, b: str) -> dict[str, ResolvedElement]:
    return {"claim_number": _el(html, a), "amount": _el(html, b)}


@pytest.mark.unit
def test_table_to_div_multi_field_extracts_every_row() -> None:
    c = _read(D, _pair(D, cell(1, 1), cell(1, 2)))
    assert [r["claim_number"].value for r in c.rows] == CLAIMS
    assert [r["amount"].value for r in c.rows] == ["1,200.00", "90.50", "7.00"]


@pytest.mark.unit
@pytest.mark.parametrize("html", [D, R])
def test_table_to_div_cross_row_is_refused(html: str) -> None:
    suffix = "/div" if html is R else ""
    with pytest.raises(RowMisaligned):
        _read(html, _pair(html, cell(1, 1) + suffix, cell(2, 2) + suffix))


@pytest.mark.unit
def test_table_to_div_sidebar_is_refused() -> None:
    with pytest.raises(RowMisaligned):
        _read(D, _pair(D, cell(1, 1), "//nav//li[1]"))


T3 = """<tr><th>Claim Number</th><th>Amount</th></tr>
<tr><td>AB12345678</td><td>1,200.00</td></tr>
<tr><td>CD87654321</td><td>90.50</td></tr>
<tr><td>EF11112222</td><td>7.00</td></tr>"""
LAYOUT = f"<html><body><div class='nav'>menu</div><div class='main'><table>{T3}</table></div></body></html>"
LAYOUT_SIDEBAR = (
    f"<html><body><div class='main'><table>{T3}</table><ul><li>9.99</li><li>1.00</li></ul></div>"
    "<div class='foot'>x</div></body></html>"
)


@pytest.mark.unit
def test_cross_row_in_a_table_inside_a_repeated_layout_div_is_refused() -> None:
    with pytest.raises(RowMisaligned):
        _read(LAYOUT, _pair(LAYOUT, "//tr[2]/td[1]", "//tr[3]/td[2]"))


@pytest.mark.unit
def test_sidebar_in_a_repeated_layout_div_is_refused() -> None:
    with pytest.raises(RowMisaligned):
        _read(LAYOUT_SIDEBAR, _pair(LAYOUT_SIDEBAR, "//tr[2]/td[1]", "//ul/li[1]"))


@pytest.mark.unit
def test_the_layout_table_still_extracts_every_row() -> None:
    c = _read(LAYOUT, _pair(LAYOUT, "//tr[2]/td[1]", "//tr[2]/td[2]"))
    assert [r["claim_number"].value for r in c.rows] == CLAIMS


@pytest.mark.unit
@pytest.mark.parametrize(
    ("html", "xpath"),
    [
        (D, cell(1, 1) + "/a"),
        (R, cell(1, 1) + "/div"),
        (BASE, "//tbody/tr[1]/td[1]/a"),
    ],
)
def test_single_field_extracts_every_data_row(html: str, xpath: str) -> None:
    c = _read(html, {"claim_number": _el(html, xpath)}, ONE_FIELD_SPEC)
    assert [r["claim_number"].value for r in c.rows] == CLAIMS


@pytest.mark.unit
def test_single_field_in_a_doubly_repeated_wrapper_is_refused() -> None:
    w = """<html><body><div class='grid'>
<div class='r'><div><span>AB12345678</span></div><div><span>1,200.00</span></div></div>
<div class='r'><div><span>CD87654321</span></div><div><span>90.50</span></div></div>
</div></body></html>"""
    with pytest.raises(RowMisaligned):
        _read(w, {"claim_number": _el(w, "//div[@class='r'][1]/div[1]/span")}, ONE_FIELD_SPEC)


@pytest.mark.unit
def test_a_target_with_no_resolved_cell_is_an_error_not_a_missing_column() -> None:
    with pytest.raises(LookupError):
        _read(D, {"claim_number": _el(D, cell(1, 1))})


@pytest.mark.unit
def test_a_node_id_that_is_not_an_element_is_refused() -> None:
    from lxml import html as lh

    from selector_drift.extract.rows import locate

    with pytest.raises(LookupError):
        locate(lh.fromstring(BASE), "//tbody/tr[1]/td[1]/a/@href")
