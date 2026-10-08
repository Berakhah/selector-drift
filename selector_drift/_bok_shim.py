"""ponytail: a local stand-in for `bok_core.findings` (and the `to_rail` that its `to_sarif` needs
from `bok_core.provenance.render`). It mirrors backoffice-kit's INTERFACE-CONTRACT.md 4 exactly --
`Severity`, `Finding`, `to_sarif` -- under the slip plan in 8, because the installed bok-core
slice does not ship `findings` yet. Upgrade path: once the slice ships it, delete this file and
swap one import per consumer (`from selector_drift._bok_shim import ...` ->
`from bok_core.findings import ...`; `to_rail` -> `bok_core.provenance.render`) in Tasks 29,
29A, 34, 34C and 36, and add "bok_core.findings" to tests/test_bok_core_allowlist.py.
tests/test_bok_shim.py pins this surface against the contract text so drift is noticed first.

Provenance is NOT shimmed: `Claim`, `DomSource` and `Method` come from the installed
`bok_core.provenance`. A shim is never acceptable for `boundary` (contract 8); nothing here
touches it. Not a copy of ground-truth's _bok_shim.py, which mirrors the stale v0.1."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from bok_core.provenance.claim import Claim
from pydantic import BaseModel, ConfigDict

SARIF_SCHEMA: Final = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_VERSION: Final = "2.1.0"

# What bok-ui's <ProvenanceRail> consumes: a JSON object per claim, parents nested (contract
# 2.4). The contract does not pin its keys; these are the Claim's own fields in JSON mode plus
# `renders_as_fact` (I7), so nothing a reader needs is dropped.
RailPayload = dict[str, Any]

__all__ = ["Finding", "RailPayload", "Severity", "to_rail", "to_sarif"]


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    severity: Severity
    title: str
    claim: Claim[Any]
    taxonomy_refs: list[str] = []
    logical_location: str | None = None
    partial_fingerprints: dict[str, str] = {}
    properties: dict[str, Any] = {}


# SARIF's `level` collapses the ladder; the ladder value rides on properties.bok_severity too.
_LEVEL: Final = {
    Severity.CRITICAL: "error",
    Severity.HIGH: "error",
    Severity.MEDIUM: "warning",
    Severity.LOW: "warning",
    Severity.INFO: "note",
}


def to_rail(claim: Claim[Any]) -> RailPayload:
    """Serialisation consumed by bok-ui's <ProvenanceRail>. Parents nest."""
    body: RailPayload = claim.model_dump(mode="json", exclude={"parents"})
    body["renders_as_fact"] = claim.renders_as_fact()
    body["parents"] = [to_rail(p) for p in claim.parents]
    return body


def _result(f: Finding) -> dict[str, Any]:
    properties = f.model_dump(mode="json")["properties"] | {
        "bok_severity": f.severity.value,
        "bok_provenance": to_rail(f.claim),
        "bok_taxonomy_refs": list(f.taxonomy_refs),
    }
    result: dict[str, Any] = {
        "ruleId": f.id,
        "level": _LEVEL[f.severity],
        "message": {"text": f.title},
    }
    if f.logical_location is not None:
        result["logicalLocations"] = [{"fullyQualifiedName": f.logical_location}]
    if f.partial_fingerprints:
        result["partialFingerprints"] = dict(f.partial_fingerprints)
    result["properties"] = properties
    return result


def to_sarif(findings: list[Finding], *, tool_name: str, tool_version: str) -> dict[str, Any]:
    """SARIF 2.1.0. logicalLocations, partialFingerprints and properties are
    first-class, not only physicalLocation.

    Every result carries properties.bok_provenance = to_rail(finding.claim) and
    properties.bok_severity = the ladder value."""
    rule_ids = list(dict.fromkeys(f.id for f in findings))
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": tool_name,
                        "version": tool_version,
                        "rules": [{"id": r} for r in rule_ids],
                    }
                },
                "results": [_result(f) for f in findings],
            }
        ],
    }
