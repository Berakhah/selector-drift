from __future__ import annotations

import pytest

from selector_drift.resolve.page import StaticPageSource
from tests.resolve.conftest import BenchPage

SEMANTICS = """<!doctype html><html><head><title>Fixture</title><style>
.gone { display: none }
.away { position: absolute; left: -10000px }
@media print { .results { display: none } }
</style></head><body>
<table id="t"><caption>Claims</caption>
<thead><tr><th>Claim Number</th><th><img alt="Amount" src="a.png"></th></tr></thead>
<tbody><tr><th scope="row">Row One</th><td>AB12345678</td></tr></tbody></table>
<div role="table"><div role="row"><div role="columnheader" id="st">Status</div></div></div>
<label for="q">Search claims</label><input id="q">
<span id="lbl">Filed on</span><input id="f" aria-labelledby="lbl">
<button id="b" aria-label="Close"><img alt="x" src="x.png"></button>
<img id="deco" alt="" src="d.png">
<p data-probe="plain">plain</p>
<p data-probe="attr" hidden>hidden attribute</p>
<div class="gone"><p data-probe="sheet">styled away</p></div>
<div aria-hidden="true"><p data-probe="aria">aria hidden</p></div>
<p data-probe="inline" style="display:none">inline none</p>
<p style="visibility:hidden"><span data-probe="vis">invisible</span>
  <span data-probe="revis" style="visibility:visible">visible again</span></p>
<p data-probe="away" class="away">off screen</p>
<p data-probe="sronly" style="position:absolute;width:1px;height:1px;overflow:hidden;
  clip:rect(0,0,0,0)">screen reader only</p>
<input data-probe="csrf" type="hidden" name="csrf" value="x">
</body></html>"""


@pytest.fixture
def page() -> StaticPageSource:
    return StaticPageSource(SEMANTICS)


@pytest.mark.unit
def test_node_id_is_exactly_lxmls_getpath(bench: BenchPage) -> None:
    """Pinned 2026-09-26: comparable with the answer key and Task 27's locate() as-is."""
    page = bench.source()
    el = page.query("tbody > tr:nth-of-type(1) > td:nth-of-type(1)")[0]
    assert page.node_id(el) == el.getroottree().getpath(el) == bench.truth("claim_number")[0]


@pytest.mark.unit
def test_node_id_is_not_derived_from_the_selector(bench: BenchPage) -> None:
    """C5. Two selectors naming the same node must yield the same node_id."""
    page = bench.source()
    a = page.query("#results td")[0]
    b = page.query("//table[@id='results']/tbody/tr[1]/td[1]")[0]
    assert page.node_id(a) == page.node_id(b)


@pytest.mark.unit
def test_a_node_id_round_trips_to_the_same_element(bench: BenchPage) -> None:
    page = bench.source()
    el = page.query("tbody td")[3]
    assert page.node(page.node_id(el)) is el


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["//*", "n1", "/html/body/../..", "td"])
def test_node_accepts_only_a_path_it_could_have_minted(bench: BenchPage, bad: str) -> None:
    assert bench.source().node(bad) is None


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["td[", "//td[", "td:hover-ish("])
def test_a_broken_selector_matches_nothing_rather_than_raising(
    page: StaticPageSource, bad: str
) -> None:
    assert page.query(bad) == []


@pytest.mark.unit
def test_an_empty_dom_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        StaticPageSource("  ")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("selector", "role", "name"),
    [
        ("#t", "table", "Claims"),
        ("thead th:nth-of-type(1)", "columnheader", "Claim Number"),
        ("thead th:nth-of-type(2)", "columnheader", "Amount"),  # ImageHeader: alt text
        ("tbody th", "rowheader", "Row One"),
        ("tbody td", "cell", "AB12345678"),
        ("#st", "columnheader", "Status"),  # TableToDiv's roles
        ("#q", "textbox", "Search claims"),  # <label for>
        ("#f", "textbox", "Filed on"),  # aria-labelledby
        ("#b", "button", "Close"),  # aria-label beats content
    ],
)
def test_roles_and_accessible_names(
    page: StaticPageSource, selector: str, role: str, name: str
) -> None:
    el = page.query(selector)[0]
    assert page.role(el) == role
    assert page.name(el) == name


@pytest.mark.unit
def test_an_image_with_empty_alt_is_presentational(page: StaticPageSource) -> None:
    assert page.role(page.query("#deco")[0]) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("probe", "visible"),
    [
        ("plain", True),
        ("attr", False),
        ("sheet", False),
        ("aria", False),
        ("inline", False),
        ("vis", False),
        ("revis", True),
        ("away", False),
        ("sronly", False),
        ("csrf", False),
    ],
)
def test_visibility_is_the_h9_precondition(
    page: StaticPageSource, probe: str, visible: bool
) -> None:
    """H9: rendered, not aria-hidden, a real box, on the page."""
    el = page.query(f'[data-probe="{probe}"]')[0]
    assert page.visible(el) is visible


@pytest.mark.unit
def test_a_media_query_is_not_applied_statically(bench: BenchPage) -> None:
    html = bench.html.replace(
        "</head>", "<style>@media print { table { display: none } }</style></head>"
    )
    page = StaticPageSource(html)
    assert page.visible(page.query("#results")[0])


@pytest.mark.unit
def test_the_ax_tree_excludes_what_is_hidden_and_keeps_what_is_named(
    page: StaticPageSource,
) -> None:
    nodes = list(page.ax().walk())
    assert ("columnheader", "Claim Number") in {(n.role, n.name) for n in nodes}
    assert not any("aria hidden" in n.name or "styled away" in n.name for n in nodes)
    assert all(page.node(n.node_id) is not None for n in nodes)
