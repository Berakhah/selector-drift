"""Task 26C: the bench scorer judges every bench heal against the answer key."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import lxml.html
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from testbench.score import Judgement, UnscorableHeal, judge, seed_day, truth_path
from tests.testbench.conftest import SeedDecoy, SeededHeal


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
def test_accepted_is_the_products_own_predicate_not_a_copy() -> None:
    """Fix round 1 (I2): the scorer reuses orchestrate.history's predicate; the integration
    tests below exercise it (promoted, discarded, stale row beside a healed run)."""
    from selector_drift.orchestrate import history
    from testbench import score

    assert score.accepted_run is history.accepted_run
    assert not hasattr(score, "ACCEPTED_STATES")  # no re-derived copy to drift


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


def _detail(db: OrmSession, heal: SeededHeal) -> dict[str, object]:
    detail: dict[str, object] = db.execute(
        text("select detail from audit_log where action = 'scored' and subject = :s "
             "and actor = 'testbench.score'"),
        {"s": str(heal.id)},
    ).scalar_one()  # fmt: skip
    return detail


def _scored_rows(db: OrmSession) -> int:
    return int(db.execute(text("select count(*) from audit_log where action = 'scored'")).one()[0])


def _resolve(db: OrmSession, run_key: str, resolved_as: str) -> None:
    db.execute(
        text("update quarantine set resolved_as = :r, actor = 'tester', resolved_at = now() "
             "where run_key = :k"),
        {"r": resolved_as, "k": run_key},
    )  # fmt: skip
    db.commit()


@pytest.mark.integration
def test_an_undecided_quarantine_stays_pending_then_a_promotion_scores_false_accept(
    db: OrmSession, decoy_heal_quarantined: SeededHeal
) -> None:
    """Fix round 1 (C1): scoring before the human decides would freeze a later promotion of
    the decoy as a correct refusal. Pending means no judgement and no audit row."""
    from testbench.score import score_pending

    assert score_pending(db) == []
    assert _scored_rows(db) == 0
    _resolve(db, decoy_heal_quarantined.run_key, "promoted")
    assert score_pending(db) == [decoy_heal_quarantined.id]
    detail = _detail(db, decoy_heal_quarantined)
    assert detail["judgement"] == Judgement.FALSE_ACCEPT.value
    assert detail["run_key"] == decoy_heal_quarantined.run_key  # the pinned row shape


@pytest.mark.integration
def test_a_discarded_decoy_heal_scores_as_a_correct_refusal(
    db: OrmSession, decoy_heal_quarantined: SeededHeal
) -> None:
    from testbench.score import score_pending

    _resolve(db, decoy_heal_quarantined.run_key, "discarded")
    assert score_pending(db) == [decoy_heal_quarantined.id]
    detail = _detail(db, decoy_heal_quarantined)
    assert detail["judgement"] == Judgement.CORRECT_REFUSAL.value
    assert detail["run_key"] == decoy_heal_quarantined.run_key


@pytest.mark.integration
def test_a_run_with_no_terminal_state_stays_pending(db: OrmSession, seed_decoy: SeedDecoy) -> None:
    """record_attempt holds an in-progress (or retrying) run at failed/NO_TERMINAL_STATE: that
    is not a refusal, and a later `healed` settle of a wrong heal must still be caught."""
    from selector_drift.orchestrate.run_record import NO_TERMINAL_STATE
    from testbench.score import score_pending

    heal = seed_decoy(state="failed", error=NO_TERMINAL_STATE)
    assert score_pending(db) == []
    assert _scored_rows(db) == 0
    db.execute(
        text("update runs set state = 'healed', error = null where run_key = :k"),
        {"k": heal.run_key},
    )
    db.commit()
    assert score_pending(db) == [heal.id]
    assert _detail(db, heal)["judgement"] == Judgement.FALSE_ACCEPT.value


@pytest.mark.integration
def test_a_decoy_heal_the_gate_let_through_is_a_false_accept(
    db: OrmSession, seed_decoy: SeedDecoy
) -> None:
    """Fix round 1 (I3): the failure this product exists to prevent, end to end."""
    from testbench.score import score_pending

    heal = seed_decoy(state="healed")  # no quarantine row: the gate passed it
    assert score_pending(db) == [heal.id]
    detail = _detail(db, heal)
    assert detail["judgement"] == Judgement.FALSE_ACCEPT.value
    assert detail["run_key"] == heal.run_key


@pytest.mark.integration
def test_a_promoted_decoy_heal_is_a_false_accept(db: OrmSession, seed_decoy: SeedDecoy) -> None:
    from testbench.score import score_pending

    heal = seed_decoy(state="quarantined", quarantine="promoted")
    assert score_pending(db) == [heal.id]
    detail = _detail(db, heal)
    assert detail["judgement"] == Judgement.FALSE_ACCEPT.value
    assert detail["run_key"] == heal.run_key


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


# ---------------------------------------------------------------- fix round 2: hardening


def _all_details(db: OrmSession) -> list[str]:
    return [str(d) for d in db.execute(text("select detail::text from audit_log")).scalars().all()]


@pytest.mark.integration
def test_a_poison_heal_does_not_stop_the_others(
    db: OrmSession,
    seeded_bench_heals: list[SeededHeal],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Round 2 (1): a scorer bug on one heal rolls back that heal's savepoint only. It stays
    pending with no audit row, the rest are committed, and the call then fails loudly."""
    import testbench.score as score

    real = score.expected_xpaths
    poison = seeded_bench_heals[1]  # the "amount" heal

    def boom(day: dt.date, field: str, page: int = 1) -> list[str]:
        if field == "amount":
            raise RuntimeError("SENTINEL-poison-message")
        return real(day, field, page)

    monkeypatch.setattr(score, "expected_xpaths", boom)
    with pytest.raises(score.ScorerFailed) as raised:
        score.score_pending(db)
    assert raised.value.heal_ids == [poison.id]
    others = {h.id for h in seeded_bench_heals} - {poison.id}
    subjects = db.execute(
        text("select subject from audit_log where action = 'scored'")
    ).scalars().all()  # fmt: skip
    assert {int(s) for s in subjects} == others  # committed; the poison heal unrecorded
    assert "RuntimeError" in caplog.text and str(poison.id) in caplog.text
    assert "SENTINEL-poison-message" not in caplog.text  # class name only, never the message
    monkeypatch.setattr(score, "expected_xpaths", real)
    assert score.score_pending(db) == [poison.id]  # still pending, scored once fixed


@pytest.mark.integration
def test_an_overlapping_scorer_waits_and_never_double_scores(
    db: OrmSession, seeded_bench_heals: list[SeededHeal], redaction_key: bytes
) -> None:
    """Round 2 (2): every score_pending takes one advisory transaction lock first. Here a
    second session holds the lock; a scorer started in a thread blocks on it, the holder
    scores everything and commits (releasing the lock), and the waiter's query -- a new
    READ COMMITTED snapshot taken after the lock -- then finds nothing pending."""
    import threading

    from selector_drift.db.session import Session as DbSession
    from testbench.score import SCORER_LOCK_KEY, score_pending

    result: dict[str, object] = {}

    def waiter() -> None:
        with DbSession() as other:
            result["ids"] = score_pending(other, redaction_key=redaction_key)

    with DbSession() as holder:
        holder.execute(text("select pg_advisory_xact_lock(:k)"), {"k": SCORER_LOCK_KEY})
        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        thread.join(timeout=2)
        assert thread.is_alive()  # blocked on the lock, not scoring
        first = score_pending(holder, redaction_key=redaction_key)  # commits, releases the lock
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert sorted(first) == sorted(h.id for h in seeded_bench_heals)
    assert result["ids"] == []
    counts = db.execute(
        text("select subject, count(*) from audit_log where action = 'scored' group by subject")
    ).all()
    assert all(n == 1 for _, n in counts) and len(counts) == len(seeded_bench_heals)


@pytest.mark.integration
def test_a_stored_uri_naming_another_bundle_is_unscorable(
    db: OrmSession, seeded_bench_heals: list[SeededHeal]
) -> None:
    """Round 2 (3): the bundle is derived from the heal id, never trusted from the row."""
    from testbench.score import score_pending

    victim, foreign = seeded_bench_heals[0], seeded_bench_heals[1]
    foreign_uri = db.execute(
        text("select evidence_uri from heal_events where id = :i"), {"i": foreign.id}
    ).scalar_one()
    db.execute(
        text("update heal_events set evidence_uri = :u where id = :i"),
        {"u": foreign_uri, "i": victim.id},
    )
    db.commit()
    score_pending(db)
    detail = _detail(db, victim)
    assert detail["judgement"] == "unscorable"
    assert "evidence outside the store" in str(detail["reason"])
    assert foreign_uri not in json.dumps(detail) and str(foreign.id) not in str(detail["reason"])
    assert _detail(db, foreign)["judgement"] == foreign.expected  # its own bundle still scores


def _replace_after_html(bundle: Path, data: bytes) -> None:
    from selector_drift.heallog.bundle import AFTER_HTML, MANIFEST, file_digest

    (bundle / AFTER_HTML).write_bytes(data)
    manifest = json.loads((bundle / MANIFEST).read_text(encoding="utf-8"))
    manifest["files"][AFTER_HTML] = file_digest(data)  # still passes its manifest hash
    (bundle / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


SENTINEL = "SENTINEL-zq9-page-text"


@pytest.mark.integration
def test_a_page_that_cannot_be_decoded_never_leaks_into_the_reason(
    db: OrmSession, seeded_bench_heals: list[SeededHeal]
) -> None:
    """Round 2 (4): only UnscorableHeal and EvidenceCorrupted keep their authored message;
    anything else records its type name alone."""
    from testbench.score import score_pending

    heal = seeded_bench_heals[0]
    uri = db.execute(
        text("select evidence_uri from heal_events where id = :i"), {"i": heal.id}
    ).scalar_one()
    _replace_after_html(Path(uri), f"<html><body>{SENTINEL}".encode() + b"\xff\xfe" + b"</body>")
    score_pending(db)
    detail = _detail(db, heal)
    assert detail["judgement"] == "unscorable"
    assert detail["reason"] == "UnicodeDecodeError"
    assert not any(SENTINEL in d for d in _all_details(db))


@pytest.mark.integration
def test_an_evidence_error_message_is_reduced_to_its_type(
    db: OrmSession, seeded_bench_heals: list[SeededHeal], monkeypatch: pytest.MonkeyPatch
) -> None:
    import testbench.score as score

    def leaky(*args: object, **kwargs: object) -> bytes:
        raise OSError(f"cannot read: {SENTINEL}")

    monkeypatch.setattr(score, "verified_bytes", leaky)
    score.score_pending(db)
    for heal in seeded_bench_heals:
        assert _detail(db, heal) == {
            "field": _detail(db, heal)["field"],
            "judgement": "unscorable",
            "reason": "OSError",
            "run_key": heal.run_key,
        }
    assert not any(SENTINEL in d for d in _all_details(db))
