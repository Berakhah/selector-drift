from __future__ import annotations

import pytest
from bok_core.provenance.claim import Method
from bok_core.provenance.source import DomSource

from selector_drift.extract.reader import RowMisaligned, read
from selector_drift.resolve.trace import ResolvedElement
from tests.extract.conftest import (
    AT,
    ONE_FIELD_SPEC,
    SPEC,
    TABLE,
    TABLE_NESTED,
    TABLE_PLUS_SIDEBAR,
    TABLE_THREE_ROWS,
    TABLE_WITH_PAGER,
    TRACES,
)

SNAP = "s" * 64


@pytest.mark.unit
def test_every_extracted_value_is_a_deterministic_claim_with_no_confidence(
    table_resolved: dict[str, ResolvedElement],
) -> None:
    c = read(
        TABLE,
        table_resolved,
        SPEC,
        run_key="testbench:1:2026-10-01",
        snapshot_id=SNAP,
        traces=TRACES,
        observed_at=AT,
    )
    assert [r["claim_number"].value for r in c.rows] == ["AB12345678", "CD87654321"]
    assert [r["amount"].value for r in c.rows] == ["1,200.00", "90.50"]
    assert c.run_key == "testbench:1:2026-10-01" and c.spec_version == 1
    for row in c.rows:
        for claim in row.values():
            assert claim.method is Method.DETERMINISTIC and claim.confidence is None


@pytest.mark.unit
def test_every_claim_points_back_to_the_snapshot_it_came_from(
    table_resolved: dict[str, ResolvedElement],
) -> None:
    c = read(
        TABLE, table_resolved, SPEC, run_key="k", snapshot_id=SNAP, traces=TRACES, observed_at=AT
    )
    claim = c.rows[0]["amount"]
    assert isinstance(claim.source, DomSource)
    assert claim.source.snapshot_id == SNAP
    assert claim.source.rung == table_resolved["amount"].rung_id


@pytest.mark.unit
def test_fields_that_do_not_share_a_row_are_refused_not_zipped(
    misaligned_resolved: dict[str, ResolvedElement],
) -> None:
    with pytest.raises(RowMisaligned):
        read(
            TABLE_PLUS_SIDEBAR,
            misaligned_resolved,
            SPEC,
            run_key="k",
            snapshot_id=SNAP,
            traces=TRACES,
            observed_at=AT,
        )


@pytest.mark.unit
def test_a_field_resolved_in_another_data_row_is_refused(
    cross_row_resolved: dict[str, ResolvedElement],
) -> None:
    with pytest.raises(RowMisaligned):
        read(
            TABLE_THREE_ROWS,
            cross_row_resolved,
            SPEC,
            run_key="k",
            snapshot_id=SNAP,
            traces=TRACES,
            observed_at=AT,
        )


@pytest.mark.unit
def test_nested_cell_markup_extracts_every_row(nested_resolved: dict[str, ResolvedElement]) -> None:
    c = read(
        TABLE_NESTED,
        nested_resolved,
        SPEC,
        run_key="k",
        snapshot_id=SNAP,
        traces=TRACES,
        observed_at=AT,
    )
    assert [(r["claim_number"].value, r["amount"].value) for r in c.rows] == [
        ("AB12345678", "1,200.00"),
        ("CD87654321", "90.50"),
    ]


@pytest.mark.unit
def test_a_single_wrapped_field_still_yields_one_value_per_row(
    wrapped_single_resolved: dict[str, ResolvedElement],
) -> None:
    c = read(
        TABLE_NESTED,
        wrapped_single_resolved,
        ONE_FIELD_SPEC,
        run_key="k",
        snapshot_id=SNAP,
        traces=TRACES,
        observed_at=AT,
    )
    assert [r["claim_number"].value for r in c.rows] == ["AB12345678", "CD87654321"]


@pytest.mark.unit
def test_a_pagination_row_is_not_a_data_row(
    table_with_pager_resolved: dict[str, ResolvedElement],
) -> None:
    c = read(
        TABLE_WITH_PAGER,
        table_with_pager_resolved,
        SPEC,
        run_key="k",
        snapshot_id=SNAP,
        traces=TRACES,
        observed_at=AT,
    )
    assert [r["claim_number"].value for r in c.rows] == ["AB12345678", "CD87654321"]


@pytest.mark.unit
def test_an_unlocatable_node_raises_instead_of_extracting_nothing(
    table_resolved: dict[str, ResolvedElement],
) -> None:
    gone = ResolvedElement(
        selector="x",
        node_id="/html/body/table/tr[9]/td[2]",
        anchor=table_resolved["amount"].anchor,
        rung_id=3,
    )
    with pytest.raises(LookupError):
        read(
            TABLE,
            {**table_resolved, "amount": gone},
            SPEC,
            run_key="k",
            snapshot_id=SNAP,
            traces=TRACES,
            observed_at=AT,
        )
