from __future__ import annotations

import pytest
from lxml import html as lh

from selector_drift.extract.rows import follow, locate, rel_path, row_of

TABLE = """<html><body><table>
<tr><th>Claim Number</th><th>Amount</th></tr>
<tr><td>AB12345678</td><td>1,200.00</td></tr>
<tr><td>CD87654321</td><td>90.50</td></tr>
</table></body></html>"""

DIVS = """<html><body><div class="grid">
<div class="r"><span>Claim Number</span><span>Amount</span></div>
<div class="r"><span>AB12345678</span><span>1,200.00</span></div>
<div class="r"><span>CD87654321</span><span>90.50</span></div>
</div></body></html>"""


@pytest.mark.unit
@pytest.mark.parametrize(
    ("html", "cell"),
    [(TABLE, "//tr[2]/td[2]"), (DIVS, "//div[@class='r'][2]/span[2]")],
)
def test_the_row_model_survives_table_to_div(html: str, cell: str) -> None:
    root = lh.fromstring(html)
    el = root.xpath(cell)[0]
    row = row_of(el)
    path = rel_path(row, el)
    parent = row.getparent()
    assert parent is not None
    values = [c.text_content() for r in parent if (c := follow(r, path)) is not None]
    assert values[-2:] == ["1,200.00", "90.50"]


@pytest.mark.unit
def test_a_cell_is_never_its_own_row() -> None:
    el = lh.fromstring(TABLE).xpath("//tr[2]/td[1]")[0]
    assert row_of(el).tag == "tr"


@pytest.mark.unit
def test_a_wrapped_single_cell_resolves_to_the_tr_not_the_td() -> None:
    root = lh.fromstring(
        "<table><tr><td><span>v</span></td><td>w</td></tr><tr><td>x</td></tr></table>"
    )
    assert row_of(root.xpath("//tr[1]/td[1]/span")[0]).tag == "tr"


@pytest.mark.unit
def test_locate_finds_a_static_node_by_xpath_and_a_live_one_by_attribute() -> None:
    root = lh.fromstring('<html><body><p data-sd-node="n-3">x</p></body></html>')
    assert locate(root, "n-3").text == "x"
    assert locate(root, root.getroottree().getpath(root.xpath("//p")[0])).text == "x"


@pytest.mark.unit
def test_locate_refuses_a_node_found_zero_or_twice() -> None:
    root = lh.fromstring(
        '<html><body><p data-sd-node="a">x</p><p data-sd-node="a">y</p></body></html>'
    )
    with pytest.raises(LookupError):
        locate(root, "a")
    with pytest.raises(LookupError):
        locate(root, "missing")
