"""Shared fixtures for the extraction tests: one table, its look-alikes, and resolved cells."""

from __future__ import annotations

import datetime as dt

import pytest
from lxml import html as lh

from selector_drift.resolve.trace import AnchorEvidence, CascadeTrace, ResolvedElement
from selector_drift.spec.schema import TaskSpec

AT = dt.datetime(2026, 10, 1, 4, 12, tzinfo=dt.UTC)
TRACES: dict[str, CascadeTrace] = {}
SPEC = TaskSpec.model_validate(
    {
        "version": 1,
        "scope_ref": "scopes/testbench.yaml",
        "schedule": "12 4 * * *",
        "row_scope": "each row",
        "natural_key": ["claim_number"],
        "expected_schema": {"claim_number": "string", "amount": "decimal"},
        "navigate": [],
        "targets": [
            {"name": "claim_number", "description": "claim number", "assertions": []},
            {"name": "amount", "description": "amount", "assertions": []},
        ],
    }
)
ONE_FIELD_SPEC = TaskSpec.model_validate(
    {
        "version": 1,
        "scope_ref": "scopes/testbench.yaml",
        "schedule": "12 4 * * *",
        "row_scope": "each row",
        "natural_key": ["claim_number"],
        "expected_schema": {"claim_number": "string"},
        "navigate": [],
        "targets": [{"name": "claim_number", "description": "claim number", "assertions": []}],
    }
)
TABLE = """<html><body><table>
<tr><th>Claim Number</th><th>Amount</th></tr>
<tr><td>AB12345678</td><td>1,200.00</td></tr>
<tr><td>CD87654321</td><td>90.50</td></tr></table></body></html>"""
TABLE_WITH_PAGER = TABLE.replace(
    "</table>", "<tr><td colspan='2'><a>1</a> <a>2</a></td></tr></table>"
)
TABLE_PLUS_SIDEBAR = TABLE.replace("</body>", "<ul><li>9.99</li><li>1.00</li></ul></body>")
TABLE_NESTED = """<html><body><table>
<tr><th>Claim Number</th><th>Amount</th></tr>
<tr><td><span>AB12345678</span></td><td><b>1,200.00</b></td></tr>
<tr><td><span>CD87654321</span></td><td><b>90.50</b></td></tr></table></body></html>"""
TABLE_THREE_ROWS = """<html><body><table>
<tr><th>Claim Number</th><th>Amount</th></tr>
<tr><td>AB12345678</td><td>1,200.00</td></tr>
<tr><td>CD87654321</td><td>90.50</td></tr>
<tr><td>EF11112222</td><td>7.00</td></tr></table></body></html>"""


def _el(html: str, xpath: str, rung: int = 3) -> ResolvedElement:
    root = lh.fromstring(html)
    node = root.xpath(xpath)[0]
    return ResolvedElement(
        selector=xpath,
        node_id=root.getroottree().getpath(node),
        anchor=AnchorEvidence(accessible_name=None, label_text=None, header_text=None),
        rung_id=rung,
    )


@pytest.fixture
def table_resolved() -> dict[str, ResolvedElement]:
    return {"claim_number": _el(TABLE, "//tr[2]/td[1]"), "amount": _el(TABLE, "//tr[2]/td[2]", 4)}


@pytest.fixture
def table_with_pager_resolved() -> dict[str, ResolvedElement]:
    return {
        "claim_number": _el(TABLE_WITH_PAGER, "//tr[2]/td[1]"),
        "amount": _el(TABLE_WITH_PAGER, "//tr[2]/td[2]"),
    }


@pytest.fixture
def misaligned_resolved() -> dict[str, ResolvedElement]:
    return {
        "claim_number": _el(TABLE_PLUS_SIDEBAR, "//tr[2]/td[1]"),
        "amount": _el(TABLE_PLUS_SIDEBAR, "//ul/li[1]"),
    }


@pytest.fixture
def nested_resolved() -> dict[str, ResolvedElement]:
    return {
        "claim_number": _el(TABLE_NESTED, "//tr[2]/td[1]/span"),
        "amount": _el(TABLE_NESTED, "//tr[2]/td[2]/b"),
    }


@pytest.fixture
def cross_row_resolved() -> dict[str, ResolvedElement]:
    """The key is in row 2 and the amount in row 3: two unrelated cells."""
    return {
        "claim_number": _el(TABLE_THREE_ROWS, "//tr[2]/td[1]"),
        "amount": _el(TABLE_THREE_ROWS, "//tr[3]/td[2]"),
    }


@pytest.fixture
def wrapped_single_resolved() -> dict[str, ResolvedElement]:
    return {"claim_number": _el(TABLE_NESTED, "//tr[2]/td[1]/span")}
