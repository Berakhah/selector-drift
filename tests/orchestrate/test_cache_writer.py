"""The write-back, against Postgres. Written in Task 17; lands and runs with Task 26A,
because ValidatedExtraction (Task 20) and CandidateExtraction (Task 27) must exist first."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import delete

from selector_drift.db.models import SelectorCacheEntry
from selector_drift.db.session import Session
from selector_drift.extract.models import CandidateExtraction
from selector_drift.orchestrate.cache_writer import CacheWriter
from selector_drift.resolve.cache import NO_CACHE, SelectorCache
from selector_drift.resolve.trace import AnchorEvidence, ResolvedElement
from selector_drift.spec.schema import FieldTarget
from selector_drift.validate.gate import HealContext, ValidatedExtraction, gate
from selector_drift.validate.history import History
from selector_drift.validate.quarantine import QuarantinedRun
from selector_drift.validate.verdict import Outcome, Verdict

PORTAL = "cache-writer-test"  # never a real target
RUN_KEY = f"{PORTAL}:2:2026-10-01"
TARGET = FieldTarget(name="claim_number", description="the claim number cell", assertions=[])
BEFORE = ResolvedElement(
    selector="html > body:nth-of-type(1) > table:nth-of-type(1)",
    node_id="/html/body/table",
    anchor=AnchorEvidence(accessible_name="Claim Number"),
    rung_id=2,
)
HEALED = ResolvedElement(
    selector="html > body:nth-of-type(1) > div:nth-of-type(1)",
    node_id="/html/body/div",
    anchor=AnchorEvidence(header_text="Claim No."),
    rung_id=4,
)


class Always:
    def __init__(self, outcome: Outcome) -> None:
        self.name = f"Always{outcome.value}"
        self._outcome = outcome

    def check(
        self, candidate: CandidateExtraction, history: History | None, ctx: HealContext
    ) -> Verdict:
        return Verdict(
            check=self.name, outcome=self._outcome, threshold=None, observed=None, reason="fixture"
        )


def judge(outcome: Outcome) -> ValidatedExtraction | QuarantinedRun:
    candidate = CandidateExtraction(rows=[], trace={}, spec_version=2, run_key=RUN_KEY)
    ctx = HealContext(healed=True, winning_rung=4, page=None, target=TARGET, cache=NO_CACHE)
    return gate(candidate, None, ctx, [Always(outcome)])


@pytest.fixture
def writer() -> Iterator[CacheWriter]:
    def clear() -> None:
        with Session() as s:
            s.execute(delete(SelectorCacheEntry).where(SelectorCacheEntry.target == PORTAL))
            s.commit()

    clear()
    yield CacheWriter.for_run_key(RUN_KEY)
    clear()


@pytest.mark.integration
def test_put_stores_the_anchor_from_the_healing_rung(writer: CacheWriter) -> None:
    validated = judge(Outcome.PASS)
    assert isinstance(validated, ValidatedExtraction)
    writer.put(validated, HEALED, field="claim_number")
    got = SelectorCache().get(PORTAL, "claim_number", spec_version=2)
    assert got is not None
    assert (got.selector, got.anchor, got.set_by_rung) == (HEALED.selector, HEALED.anchor, 4)
    with Session() as s:
        row = s.get(SelectorCacheEntry, (PORTAL, "claim_number", 2))
        assert row is not None and row.set_by_run_key == RUN_KEY


@pytest.mark.integration
def test_a_quarantined_heal_does_not_update_the_cache(writer: CacheWriter) -> None:
    """R11/C2. The single most dangerous ordering bug available in this design: a rejected
    heal that poisons the cache becomes a rung-1 hit tomorrow, records no heal, and never
    engages the strict path again."""
    accepted = judge(Outcome.PASS)
    assert isinstance(accepted, ValidatedExtraction)
    writer.put(accepted, BEFORE, field="claim_number")
    before = SelectorCache().get(PORTAL, "claim_number", spec_version=2)
    refused = judge(Outcome.FAIL)
    assert isinstance(refused, QuarantinedRun)
    with pytest.raises(TypeError):
        writer.put(refused, HEALED, field="claim_number")  # type: ignore[arg-type]
    assert SelectorCache().get(PORTAL, "claim_number", spec_version=2) == before


@pytest.mark.integration
def test_put_cannot_be_called_with_anything_but_a_validated_extraction(
    writer: CacheWriter,
) -> None:
    candidate = CandidateExtraction(rows=[], trace={}, spec_version=2, run_key=RUN_KEY)
    with pytest.raises(TypeError):
        writer.put(candidate, HEALED, field="claim_number")  # type: ignore[arg-type]
    assert SelectorCache().get(PORTAL, "claim_number", spec_version=2) is None


@pytest.mark.unit
def test_a_writer_refuses_a_validated_extraction_for_another_run_target() -> None:
    """Added in 26A: the writer is bound to (target, spec_version); a validated run of another
    target or spec version must not be able to write this target's cache."""
    validated = judge(Outcome.PASS)
    assert isinstance(validated, ValidatedExtraction)
    other = CacheWriter(target="another-target", spec_version=2)
    with pytest.raises(ValueError, match="another-target"):
        other.put(validated, HEALED, field="claim_number")
    with pytest.raises(ValueError, match="spec version"):
        CacheWriter(target=PORTAL, spec_version=3).put(validated, HEALED, field="claim_number")
