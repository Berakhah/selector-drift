"""Task 26C: the bench scorer judges every bench heal against the answer key."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
from pathlib import Path

import lxml.html
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from testbench.score import Judgement, UnscorableHeal, judge, seed_day, truth_path
from tests.testbench.conftest import SeededHeal


def _seed(day: dt.date) -> str:
    return hashlib.sha256(f"{day.isoformat()}:testbench".encode()).hexdigest()


@pytest.mark.unit
def test_seed_day_prefers_the_capture_date() -> None:
    day = dt.date(2026, 10, 3)
    at = dt.datetime(2026, 10, 3, 4, 12, tzinfo=dt.UTC)
    assert seed_day(_seed(day), at) == day


@pytest.mark.unit
def test_seed_day_recovers_a_page_served_before_midnight() -> None:
    served = dt.date(2026, 10, 2)
    at = dt.datetime(2026, 10, 3, 0, 0, 5, tzinfo=dt.UTC)
    assert seed_day(_seed(served), at) == served


@pytest.mark.unit
def test_an_unexplained_seed_is_unscorable_not_guessed() -> None:
    at = dt.datetime(2026, 10, 3, 4, 12, tzinfo=dt.UTC)
    with pytest.raises(UnscorableHeal):
        seed_day(_seed(dt.date(2026, 9, 1)), at)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("correct", "accepted", "expected"),
    [
        (True, True, Judgement.TRUE_ACCEPT),
        (False, False, Judgement.CORRECT_REFUSAL),
        (False, True, Judgement.FALSE_ACCEPT),
        (True, False, Judgement.FALSE_REFUSAL),
    ],
)
def test_judge_covers_all_four_cells(correct: bool, accepted: bool, expected: Judgement) -> None:
    assert judge(correct=correct, accepted=accepted) is expected


@pytest.mark.unit
def test_truth_path_passes_a_static_node_id_through() -> None:
    html = "<html><body><table><tr><td>AB12345678</td></tr></table></body></html>"
    assert truth_path(html, "/html/body/table/tr/td") == "/html/body/table/tr/td"


@pytest.mark.unit
def test_truth_path_finds_a_live_node_id_by_its_attribute() -> None:
    html = (
        '<html><body><table><tr><td data-sd-node="n-7">AB12345678</td></tr></table></body></html>'
    )
    assert truth_path(html, "n-7") == "/html/body/table/tr/td"


@pytest.mark.unit
def test_truth_path_refuses_a_live_node_it_cannot_find_exactly_once() -> None:
    html = '<html><body><p data-sd-node="n-1">a</p><p data-sd-node="n-1">b</p></body></html>'
    with pytest.raises(UnscorableHeal):
        truth_path(html, "n-1")


# ---------------------------------------------------------------- against the real redaction
#
# Found while implementing (2026-10-17): evidence redaction is value-level over the lxml tree
# (Task 33 fix round 1) and it redacts attribute values too -- the sd-mutation-seed tag's hex
# digest included, which the IBAN and digit-run detectors almost always match. So a stored
# after.html never carries the raw seed; these tests run the product's own redaction path.


def _paths(html: str) -> list[str]:
    root = lxml.html.document_fromstring(html)
    tree = root.getroottree()
    return [tree.getpath(e) for e in root.iter() if isinstance(e.tag, str)]


@pytest.mark.unit
@pytest.mark.parametrize("decoy", [False, True], ids=["plain-day", "decoy-day"])
def test_redaction_does_not_move_nodes(redaction_key: bytes, decoy: bool) -> None:
    # If this ever fails, every judgement the scorer makes is silently wrong.
    from tests.testbench.conftest import DECOY_DAY, PLAIN_DAY, bench_page, redacted_after

    day = DECOY_DAY if decoy else PLAIN_DAY
    raw = bench_page(day)
    red = redacted_after(raw, day, redaction_key)
    assert red != raw  # the redactor did change values (else this test proves nothing)
    assert _paths(raw) == _paths(red)


@pytest.mark.unit
def test_seed_day_recovers_the_day_from_a_redacted_page(redaction_key: bytes) -> None:
    from testbench.score import meta_seed
    from tests.testbench.conftest import PLAIN_DAY, bench_page, redacted_after

    red = redacted_after(bench_page(PLAIN_DAY), PLAIN_DAY, redaction_key)
    stored = meta_seed(red)
    assert stored.split(";", 1)[0] != _seed(PLAIN_DAY)  # the raw seed did not survive
    at = dt.datetime.combine(PLAIN_DAY + dt.timedelta(days=1), dt.time(0, 0, 5), tzinfo=dt.UTC)
    assert seed_day(stored, at, key=redaction_key) == PLAIN_DAY


@pytest.mark.unit
def test_a_redacted_seed_under_another_key_is_unscorable(redaction_key: bytes) -> None:
    from testbench.score import meta_seed
    from tests.testbench.conftest import PLAIN_DAY, bench_page, redacted_after

    stored = meta_seed(redacted_after(bench_page(PLAIN_DAY), PLAIN_DAY, redaction_key))
    at = dt.datetime.combine(PLAIN_DAY, dt.time(4, 12), tzinfo=dt.UTC)
    with pytest.raises(UnscorableHeal):
        seed_day(stored, at, key=os.urandom(32))


@pytest.mark.unit
def test_a_page_with_no_seed_tag_is_unscorable() -> None:
    from testbench.score import meta_seed

    with pytest.raises(UnscorableHeal):
        meta_seed("<html><head></head><body><p>x</p></body></html>")


@pytest.mark.unit
def test_accepted_means_what_the_product_means() -> None:
    from selector_drift.orchestrate.history import ACCEPTED_STATES
    from testbench import score

    assert score.ACCEPTED_STATES == ACCEPTED_STATES


# ---------------------------------------------------------------- the deployment


@pytest.mark.unit
def test_the_scorer_is_scheduled_after_the_bench_by_entrypoint_string() -> None:
    from selector_drift.orchestrate import deployments

    assert deployments.BENCH_SCORER == {
        "name": "bench-scorer",
        "entrypoint": "testbench/score.py:score_pending_flow",
        "cron": "42 4 * * *",
        "timezone": "UTC",
    }
    source = Path(deployments.__file__).read_text(encoding="utf-8")
    assert "import testbench" not in source and "from testbench" not in source


@pytest.mark.unit
def test_the_scorer_deployment_loads_the_flow_from_its_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from selector_drift.orchestrate.deployments import bench_scorer_deployment

    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    d = bench_scorer_deployment()
    assert d is not None
    assert d.name == "bench-scorer"
    assert d.flow_name == "bench-scorer"
    (schedule,) = d.schedules
    assert schedule.schedule.cron == "42 4 * * *"
    assert schedule.schedule.timezone == "UTC"


@pytest.mark.unit
def test_no_scorer_deployment_where_the_bench_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker image does not ship testbench/: it must still serve its own flows."""
    from selector_drift.orchestrate.deployments import bench_scorer_deployment

    monkeypatch.chdir(tmp_path)
    assert bench_scorer_deployment() is None


# ---------------------------------------------------------------- against Postgres


@pytest.mark.integration
def test_score_pending_scores_each_heal_once(
    db: OrmSession, seeded_bench_heals: list[SeededHeal]
) -> None:
    from testbench.score import score_pending

    first = score_pending(db)
    assert sorted(first) == sorted(h.id for h in seeded_bench_heals)
    assert score_pending(db) == []
    details = (
        db.execute(text("select detail from audit_log where action = 'scored'")).scalars().all()
    )
    assert {d["judgement"] for d in details} <= {j.value for j in Judgement} | {"unscorable"}


@pytest.mark.integration
def test_each_seeded_heal_gets_the_judgement_its_run_earned(
    db: OrmSession, seeded_bench_heals: list[SeededHeal]
) -> None:
    """Accepted means what the product means by it (orchestrate.history): the run settled
    completed/healed with no quarantine row, or its quarantine was promoted. A failed run that
    never wrote, and a quarantine still awaiting a human, are not accepts."""
    from testbench.score import score_pending

    score_pending(db)
    rows = db.execute(
        text("select subject, detail from audit_log where action = 'scored' "
             "and actor = 'testbench.score'")
    ).all()  # fmt: skip
    got = {int(subject): detail["judgement"] for subject, detail in rows}
    assert got == {h.id: h.expected for h in seeded_bench_heals}


@pytest.mark.integration
def test_a_quarantined_decoy_heal_scores_as_a_correct_refusal(
    db: OrmSession, decoy_heal_quarantined: SeededHeal
) -> None:
    from testbench.score import score_pending

    score_pending(db)
    detail = db.execute(
        text("select detail from audit_log where action = 'scored' and subject = :s "
             "and actor = 'testbench.score'"),
        {"s": str(decoy_heal_quarantined.id)},
    ).scalar_one()  # fmt: skip
    assert detail["judgement"] == Judgement.CORRECT_REFUSAL.value
    assert detail["run_key"] == decoy_heal_quarantined.run_key  # the pinned row shape


@pytest.mark.integration
def test_a_heal_with_no_stored_evidence_is_recorded_unscorable(
    db: OrmSession, heal_without_evidence: SeededHeal
) -> None:
    from testbench.score import score_pending

    assert score_pending(db) == [heal_without_evidence.id]
    detail = db.execute(
        text("select detail from audit_log where action = 'scored' and subject = :s"),
        {"s": str(heal_without_evidence.id)},
    ).scalar_one()
    assert detail["judgement"] == "unscorable"
    assert detail["run_key"] == heal_without_evidence.run_key
