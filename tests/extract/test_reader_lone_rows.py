"""A lone data row, a footer, a second table, and grouped cells: never zipped, never lost."""

from __future__ import annotations

import datetime as dt
import random

import pytest

from selector_drift.extract.reader import RowMisaligned, read
from selector_drift.resolve.trace import ResolvedElement
from testbench.mutations.catalogue import CATALOGUE
from testbench.mutations.table_to_div import TableToDiv
from testbench.portal.render import SearchQuery, render_results, result_set
from tests.extract.conftest import AT, ONE_FIELD_SPEC, SPEC, TRACES, _el

ONE = """<html><body><main><table><thead><tr><th>Claim Number</th><th>Amount</th></tr></thead>
<tbody><tr><td>AB12345678</td><td>1,200.00</td></tr></tbody>
<tfoot><tr><td>Total</td><td>1,200.00</td></tr></tfoot></table></main></body></html>"""
CELL = "//*[@role='rowgroup'][2]/*[@role='row'][1]/*[@role='cell']"


def _read(html: str, res: dict[str, ResolvedElement], spec=SPEC):  # type: ignore[no-untyped-def]
    return read(html, res, spec, run_key="k", snapshot_id="s" * 64, traces=TRACES, observed_at=AT)


@pytest.mark.unit
def test_a_lone_row_with_a_footer_extracts_exactly_one_row() -> None:
    d = TableToDiv().apply(ONE)
    two = {"claim_number": _el(d, CELL + "[1]"), "amount": _el(d, CELL + "[2]")}
    assert [(r["claim_number"].value, r["amount"].value) for r in _read(d, two).rows] == [
        ("AB12345678", "1,200.00")
    ]
    one = _read(d, {"claim_number": _el(d, CELL + "[1]")}, ONE_FIELD_SPEC)
    assert [r["claim_number"].value for r in one.rows] == ["AB12345678"]


@pytest.mark.unit
def test_a_footer_in_the_same_group_as_several_rows_is_skipped_only_by_shape() -> None:
    # A footer row with the same cell shape as data rows IS extracted: extraction does not
    # judge values; the gate (and Task 29's row-count contract) do.
    h = ONE.replace("<tbody>", "<tbody><tr><td>ZZ00000000</td><td>5.00</td></tr>")
    h = h.replace("</tbody>", "<tr><td colspan='2'>end</td></tr></tbody>")
    c = _read(
        h, {"claim_number": _el(h, "//tbody/tr[1]/td[1]"), "amount": _el(h, "//tbody/tr[1]/td[2]")}
    )
    assert [r["claim_number"].value for r in c.rows] == ["ZZ00000000", "AB12345678"]


@pytest.mark.unit
def test_two_sibling_one_row_tables_are_not_zipped() -> None:
    p = (
        "<html><body><main><table><tbody><tr><td>AB12345678</td><td>1,200.00</td></tr></tbody>"
        "</table><table><tbody><tr><td>XY99999999</td><td>3.00</td></tr></tbody></table>"
        "</main></body></html>"
    )
    c = _read(
        p, {"claim_number": _el(p, "//table[1]//td[1]"), "amount": _el(p, "//table[1]//td[2]")}
    )
    assert [r["claim_number"].value for r in c.rows] == ["AB12345678"]


@pytest.mark.unit
def test_a_grouped_pair_inside_a_repeated_row_is_refused() -> None:
    g = """<html><body><div class='grid'>
<div class='r'><div><span>AB12345678</span><span>1,200.00</span></div><div><span>x</span><span>y</span></div></div>
<div class='r'><div><span>CD87654321</span><span>90.50</span></div><div><span>p</span><span>q</span></div></div>
</div></body></html>"""  # noqa: E501
    res = {
        "claim_number": _el(g, "//div[@class='r'][1]/div[1]/span[1]"),
        "amount": _el(g, "//div[@class='r'][1]/div[1]/span[2]"),
    }
    with pytest.raises(RowMisaligned):
        _read(g, res)


_DAY = dt.date(2026, 10, 1)
_FIRST = result_set(_DAY)[0].claim_number
_ONE_PAGE = render_results(_DAY, query=SearchQuery.parse(claim_number=_FIRST), mark_truth=True)
_BENIGN = [e for e in CATALOGUE if not e.adversarial]


@pytest.mark.unit
@pytest.mark.parametrize("entry", _BENIGN, ids=[e.name for e in _BENIGN])
@pytest.mark.parametrize("fields", [("claim_number", "amount"), ("claim_number",)])
def test_a_one_result_page_extracts_its_single_row_under_every_benign_mutation(
    entry, fields: tuple[str, ...]
) -> None:  # type: ignore[no-untyped-def]
    from lxml import html as lh

    from selector_drift.resolve.trace import AnchorEvidence

    html = entry.build(random.Random(0)).apply(_ONE_PAGE)
    root = lh.fromstring(html)
    res = {}
    for f in fields:
        node = root.xpath(f"//*[@data-sd-truth='{f}']")[0]
        res[f] = ResolvedElement(
            selector="x",
            node_id=root.getroottree().getpath(node),
            anchor=AnchorEvidence(accessible_name=None, label_text=None, header_text=None),
            rung_id=3,
        )
    truth = [x.text_content().strip() for x in root.xpath("//*[@data-sd-truth='claim_number']")]
    c = _read(html, res, SPEC if len(fields) == 2 else ONE_FIELD_SPEC)
    assert [r["claim_number"].value for r in c.rows] == truth
