"""The read side against the real table. Postgres, so integration."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete

from selector_drift.db.models import SelectorCacheEntry
from selector_drift.db.session import Session
from selector_drift.resolve.cache import CachedSelector, SelectorCache
from selector_drift.resolve.trace import AnchorEvidence

PORTAL = "cache-test"  # never a real target, so the fixture cannot touch live rows


@pytest.fixture
def seed() -> Iterator[None]:
    def clear() -> None:
        with Session() as s:
            s.execute(delete(SelectorCacheEntry).where(SelectorCacheEntry.target == PORTAL))
            s.commit()

    clear()
    with Session() as s:
        for version, header in ((2, "Claim Number"), (3, "Claim No.")):
            s.add(
                SelectorCacheEntry(
                    target=PORTAL,
                    field="claim_number",
                    spec_version=version,
                    selector=f"html > body:nth-of-type({version})",
                    anchor=AnchorEvidence(header_text=header).model_dump(mode="json"),
                    node_id="/html/body",
                    set_by_rung=4,
                    set_at=dt.datetime.now(dt.UTC),
                    set_by_run_key=f"{PORTAL}:{version}:2026-10-01",
                )
            )
        s.commit()
    yield
    clear()


@pytest.mark.integration
def test_get_reads_back_the_anchor_that_justified_the_selector(seed: None) -> None:
    assert SelectorCache().get(PORTAL, "claim_number", 2) == CachedSelector(
        selector="html > body:nth-of-type(2)",
        anchor=AnchorEvidence(header_text="Claim Number"),
        set_by_rung=4,
    )


@pytest.mark.integration
def test_get_is_keyed_by_spec_version(seed: None) -> None:
    """M14. A spec edit changes what the field means; the old entry is not this version's."""
    assert SelectorCache().get(PORTAL, "claim_number", 4) is None


@pytest.mark.integration
def test_no_spec_version_reads_the_newest(seed: None) -> None:
    got = SelectorCache().get(PORTAL, "claim_number")
    assert got is not None and got.anchor.header_text == "Claim No."


@pytest.mark.integration
def test_an_unknown_field_is_a_miss_not_an_error(seed: None) -> None:
    assert SelectorCache().get(PORTAL, "amount", 2) is None
