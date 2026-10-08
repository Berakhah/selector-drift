"""Typed extraction: a DOM snapshot plus the cascade's resolved cells in, Claims out.

Extraction never judges a value; the validation gate does. It must, however, never zip two
unrelated lists: a mis-aligned row model is the adjacent-column failure by another route.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

from bok_core.provenance.claim import Claim, Method
from bok_core.provenance.source import DomSource
from lxml import html as lh
from lxml.html import HtmlElement

from selector_drift.extract.models import CandidateExtraction, Row
from selector_drift.extract.rows import RelPath, follow, locate, rel_path, row_of
from selector_drift.resolve.trace import CascadeTrace, ResolvedElement
from selector_drift.spec.schema import TaskSpec


class RowMisaligned(Exception):
    """Fields do not share one row. Refused, never zipped."""


def _text(el: HtmlElement) -> str:
    return " ".join(el.text_content().split())


def read(
    dom: str,
    resolved: Mapping[str, ResolvedElement],
    spec: TaskSpec,
    *,
    run_key: str,
    snapshot_id: str,
    traces: Mapping[str, CascadeTrace],
    observed_at: dt.datetime,
) -> CandidateExtraction:
    """Every row from the anchor row (the natural key's) onward, as deterministic Claims.

    Rows before the anchor are headers, by position. A row where any field's path leads
    nowhere (a pagination or footer row) is skipped. A LookupError from locate() propagates:
    a node that is gone or ambiguous is never an empty extraction."""
    root = lh.fromstring(dom)
    anchor_field = spec.natural_key[0]
    if anchor_field not in resolved:
        raise LookupError(f"natural key {anchor_field!r} was not resolved")
    cells = {name: locate(root, el.node_id) for name, el in resolved.items()}
    try:
        anchor_row = row_of(cells[anchor_field], contains=cells.values())
    except LookupError as exc:
        raise RowMisaligned(f"fields do not share one row with {anchor_field}: {exc}") from exc
    container = anchor_row.getparent()
    if container is None:  # unreachable: row_of requires a sibling, hence a parent
        raise RowMisaligned("the anchor row has no container")
    paths: dict[str, RelPath] = {name: rel_path(anchor_row, cell) for name, cell in cells.items()}

    siblings = [r for r in container if r.tag == anchor_row.tag]
    rows: list[Row] = []
    for row in siblings[siblings.index(anchor_row) :]:
        found = {name: follow(row, p) for name, p in paths.items()}
        if any(c is None for c in found.values()):
            continue
        rows.append(
            {
                name: Claim(
                    value=_text(cell),
                    method=Method.DETERMINISTIC,
                    confidence=None,
                    observed_at=observed_at,
                    source=DomSource(
                        snapshot_id=snapshot_id,
                        selector=resolved[name].selector,
                        rung=resolved[name].rung_id,
                    ),
                )
                for name, cell in found.items()
                if cell is not None
            }
        )
    return CandidateExtraction(
        rows=rows, trace=dict(traces), spec_version=spec.version, run_key=run_key
    )
