"""The local findings shim (INTERFACE-CONTRACT.md 4, slip plan 8). Its public surface is pinned
against the contract text, so drift from bok_core.findings is noticed before the import swap."""

from __future__ import annotations

import datetime as dt
import inspect
import json
from typing import Any

import pytest
from bok_core.provenance.claim import Claim, Method
from bok_core.provenance.source import DomSource

from selector_drift import _bok_shim
from selector_drift._bok_shim import Finding, Severity, to_rail, to_sarif

AT = dt.datetime(2026, 9, 7, 4, 12, tzinfo=dt.UTC)


def _cell(value: str, selector: str) -> Claim[Any]:
    return Claim(
        value=value,
        source=DomSource(snapshot_id="e5" * 32, selector=selector, rung=1),
        method=Method.DETERMINISTIC,
        confidence=None,
        observed_at=AT,
    )


def _finding(severity: Severity = Severity.HIGH, **over: Any) -> Finding:
    parents = [_cell("AB10000000", "td.claim_number"), _cell("100.00", "td.amount")]
    derived = Claim(
        value=40,
        source=parents[0].source,
        method=Method.DERIVED,
        confidence=None,
        observed_at=AT,
        parents=parents,
    )
    kw: dict[str, Any] = {
        "id": "quality/row_count/run",
        "severity": severity,
        "title": "Row count 40 is below half the trailing median (100)",
        "claim": derived,
        "taxonomy_refs": ["selector-drift:quality:row_count"],
        "logical_location": "testbench/*",
        "partial_fingerprints": {"selectorDrift/v1": "ab" * 16},
        "properties": {"rule": "row_count", "observed": "40"},
    }
    return Finding(**(kw | over))


@pytest.mark.unit
def test_the_shim_exports_exactly_the_contract_surface() -> None:
    public = {n for n in vars(_bok_shim) if not n.startswith("_")}
    assert {"Severity", "Finding", "to_sarif", "to_rail", "RailPayload"} <= public


@pytest.mark.unit
def test_severity_is_the_contract_ladder() -> None:
    assert [(s.name, s.value) for s in Severity] == [
        ("CRITICAL", "CRITICAL"),
        ("HIGH", "HIGH"),
        ("MEDIUM", "MEDIUM"),
        ("LOW", "LOW"),
        ("INFO", "INFO"),
    ]


@pytest.mark.unit
def test_finding_has_exactly_the_contract_fields_and_defaults() -> None:
    fields = Finding.model_fields
    assert list(fields) == [
        "id",
        "severity",
        "title",
        "claim",
        "taxonomy_refs",
        "logical_location",
        "partial_fingerprints",
        "properties",
    ]
    for required in ("id", "severity", "title", "claim"):
        assert fields[required].is_required()
    f = Finding(id="x", severity=Severity.INFO, title="t", claim=_cell("v", "td"))
    assert (f.taxonomy_refs, f.logical_location, f.partial_fingerprints, f.properties) == (
        [],
        None,
        {},
        {},
    )


@pytest.mark.unit
def test_to_sarif_has_the_contract_signature() -> None:
    sig = inspect.signature(to_sarif)
    assert list(sig.parameters) == ["findings", "tool_name", "tool_version"]
    assert sig.parameters["tool_name"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["tool_version"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.unit
def test_a_finding_round_trips_through_json() -> None:
    f = _finding()
    assert Finding.model_validate(json.loads(json.dumps(f.model_dump(mode="json")))) == f


@pytest.mark.unit
def test_to_sarif_is_sarif_2_1_0_with_the_tool_named() -> None:
    doc = to_sarif([_finding()], tool_name="selector-drift", tool_version="0.1.0")
    assert doc["version"] == "2.1.0"
    assert "sarif" in doc["$schema"]
    driver = doc["runs"][0]["tool"]["driver"]
    assert (driver["name"], driver["version"]) == ("selector-drift", "0.1.0")
    assert [r["id"] for r in driver["rules"]] == ["quality/row_count/run"]
    json.dumps(doc)  # serialisable as-is


@pytest.mark.unit
def test_a_result_carries_location_fingerprints_and_properties_first_class() -> None:
    f = _finding()
    (r,) = to_sarif([f], tool_name="t", tool_version="1")["runs"][0]["results"]
    assert r["ruleId"] == f.id
    assert r["message"]["text"] == f.title
    assert r["logicalLocations"] == [{"fullyQualifiedName": "testbench/*"}]
    assert r["partialFingerprints"] == f.partial_fingerprints
    assert r["properties"]["rule"] == "row_count" and r["properties"]["observed"] == "40"
    assert r["properties"]["bok_taxonomy_refs"] == ["selector-drift:quality:row_count"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("severity", "level"),
    [
        (Severity.CRITICAL, "error"),
        (Severity.HIGH, "error"),
        (Severity.MEDIUM, "warning"),
        (Severity.LOW, "warning"),
        (Severity.INFO, "note"),
    ],
)
def test_the_ladder_value_rides_on_the_result_not_only_the_level(
    severity: Severity, level: str
) -> None:
    (r,) = to_sarif([_finding(severity)], tool_name="t", tool_version="1")["runs"][0]["results"]
    assert r["level"] == level
    assert r["properties"]["bok_severity"] == severity.value


@pytest.mark.unit
def test_the_provenance_chain_travels_with_every_result() -> None:
    f = _finding()
    (r,) = to_sarif([f], tool_name="t", tool_version="1")["runs"][0]["results"]
    rail = r["properties"]["bok_provenance"]
    assert rail == to_rail(f.claim)
    assert rail["method"] == "DERIVED" and rail["value"] == 40
    assert [p["method"] for p in rail["parents"]] == ["DETERMINISTIC", "DETERMINISTIC"]
    assert rail["parents"][0]["source"] == {
        "kind": "dom",
        "snapshot_id": "e5" * 32,
        "selector": "td.claim_number",
        "rung": 1,
    }
    assert rail["parents"][0]["parents"] == []


@pytest.mark.unit
def test_a_finding_property_cannot_overwrite_the_ladder_or_the_chain() -> None:
    f = _finding(properties={"bok_severity": "INFO", "bok_provenance": "forged"})
    (r,) = to_sarif([f], tool_name="t", tool_version="1")["runs"][0]["results"]
    assert r["properties"]["bok_severity"] == "HIGH"
    assert r["properties"]["bok_provenance"] == to_rail(f.claim)


@pytest.mark.unit
def test_no_logical_location_means_no_logical_locations_entry() -> None:
    (r,) = to_sarif([_finding(logical_location=None)], tool_name="t", tool_version="1")["runs"][0][
        "results"
    ]
    assert "logicalLocations" not in r
