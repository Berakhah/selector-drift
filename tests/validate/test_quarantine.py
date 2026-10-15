# tests/validate/test_quarantine.py
import datetime as dt
import threading
import time

import pytest
from sqlalchemy import select, text

from selector_drift.db.models import AuditLog, Quarantine, Run
from selector_drift.db.session import Session, engine
from selector_drift.scope.record import FirstHealPolicy
from selector_drift.validate import quarantine as quarantine_mod
from selector_drift.validate.assertions.distribution import DistributionalAssertion
from selector_drift.validate.assertions.first_heal import FirstHealConfirmation
from selector_drift.validate.assertions.format import FormatAssertion
from selector_drift.validate.gate import ValidatedExtraction, gate_run
from selector_drift.validate.quarantine import (
    AlreadyResolved,
    GateInputs,
    NotInQuarantine,
    QuarantinedRun,
    StillInvalid,
    discard,
    mark_promoted,
    promote,
    reconfirm,
)
from selector_drift.validate.verdict import Outcome, Verdict
from tests.validate.helpers import (
    AMOUNT,
    AT,
    CLAIM_NUMBER,
    RUN_KEY,
    amount_history,
    candidate,
    column,
    ctx,
    grid,
)

ACTOR = "operator@example.invalid"
CLAIM = r"^[A-Z]{2}\d{8}$"


def first_heal_inputs(value: str = "AB12345678") -> GateInputs:
    """A heal that passed everything except FirstHealConfirmation -- the everyday quarantine."""
    return GateInputs(
        candidate=candidate([{"claim_number": value}]),
        history=None,
        contexts={"claim_number": ctx(CLAIM_NUMBER, rung=4)},
        checks={
            "claim_number": [
                FormatAssertion(CLAIM),
                FirstHealConfirmation(FirstHealPolicy.REQUIRED),
            ]
        },
    )


# ---- the pure half: reconfirm ----------------------------------------------------------------


@pytest.mark.unit
def test_a_quarantined_run_cannot_be_passed_off_as_validated():
    inputs = first_heal_inputs()
    q = gate_run(inputs.candidate, None, inputs.contexts, inputs.checks)
    assert isinstance(q, QuarantinedRun) and not isinstance(q, ValidatedExtraction)


@pytest.mark.unit
def test_promotion_re_runs_the_gate_rather_than_bypassing_it(monkeypatch):
    # M31. Patch the name in the module that CALLS it. quarantine.py does
    # `from .gate import gate_run`, which binds at import, so patching
    # selector_drift.validate.gate.gate_run -- or `gate`, which promotion never calls --
    # is invisible here, on the check that stops a human waving data through.
    seen = []
    real = quarantine_mod.gate_run

    def spy(candidate, history, contexts, checks):
        seen.append(dict(contexts))
        return real(candidate, history, contexts, checks)

    monkeypatch.setattr(quarantine_mod, "gate_run", spy)
    reconfirm(RUN_KEY, first_heal_inputs())
    assert len(seen) == 1 and all(c.human_confirmed for c in seen[0].values())


@pytest.mark.unit
def test_promotion_supplies_the_missing_evidence():
    v = reconfirm(RUN_KEY, first_heal_inputs())
    assert isinstance(v, ValidatedExtraction) and v.run_key == RUN_KEY


@pytest.mark.unit
def test_promotion_still_fails_if_a_check_still_fails():
    with pytest.raises(StillInvalid) as exc:
        reconfirm(RUN_KEY, first_heal_inputs("12345678"))
    assert [v.check for v in exc.value.failing] == ["FormatAssertion"]
    assert {v.check for v in exc.value.verdicts} == {"FormatAssertion", "FirstHealConfirmation"}


@pytest.mark.unit
def test_confirmation_tolerates_warm_up_but_not_a_shifted_distribution():
    # Warm-up is missing evidence (INSUFFICIENT); a breach is evidence against (FAIL).
    checks = {"amount": [DistributionalAssertion(warmup_runs=5)]}
    contexts = {"amount": ctx(AMOUNT, rung=4)}
    warm = GateInputs(
        candidate=column("amount", ["1,200.00"]), history=None, contexts=contexts, checks=checks
    )
    assert isinstance(reconfirm(RUN_KEY, warm), ValidatedExtraction)
    # >= MIN_VALUES (40) current values, so the shift is a judged FAIL, not INSUFFICIENT.
    shifted = GateInputs(
        candidate=column("amount", grid(5000, 9, n=40)),
        history=amount_history(40),
        contexts=contexts,
        checks=checks,
    )
    with pytest.raises(StillInvalid):
        reconfirm(RUN_KEY, shifted)


@pytest.mark.unit
def test_still_invalid_carries_every_verdict():
    """So the API's 409 can say which checks still fail (API spec 5)."""
    verdicts = [
        Verdict(check="A", outcome=Outcome.FAIL, threshold="x", observed="y", reason="r"),
        Verdict(check="B", outcome=Outcome.PASS, threshold=None, observed=None, reason="r"),
    ]
    exc = StillInvalid(verdicts)
    assert exc.verdicts == verdicts and "1 check" in str(exc)


@pytest.mark.unit
def test_inputs_rebuilt_for_another_run_are_refused():
    with pytest.raises(ValueError, match="another run"):
        reconfirm("testbench:2:2026-09-01", first_heal_inputs())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("actor", "reason"), [("", "ok"), (ACTOR, ""), (ACTOR, "   "), (ACTOR, "x" * 501)]
)
def test_a_decision_needs_an_actor_and_a_bounded_reason(actor, reason):
    with pytest.raises(ValueError):
        promote(RUN_KEY, actor=actor, reason=reason, rebuild=lambda k: first_heal_inputs())


# ---- the audited half: promote and discard ---------------------------------------------------


@pytest.fixture
def quarantined():
    with engine.begin() as c:
        c.execute(text("TRUNCATE runs, quarantine, audit_log RESTART IDENTITY CASCADE"))
    with Session() as s:
        s.add(
            Run(
                run_key=RUN_KEY,
                target="testbench",
                spec_version=2,
                window=dt.date(2026, 10, 1),
                first_started_at=AT,
                state="quarantined",
            )
        )
        s.flush()
        s.add(
            Quarantine(
                run_key=RUN_KEY,
                reason="gate failed: claim_number:FirstHealConfirmation",
                verdicts=[],
                data_uri="",
                resolved_as=None,
                actor=None,
                resolved_at=None,
            )
        )
        s.commit()
    return RUN_KEY


def _audits():
    with Session() as s:
        return list(s.scalars(select(AuditLog).order_by(AuditLog.id)))


def _row(run_key):
    with Session() as s:
        return s.get(Quarantine, run_key)


@pytest.mark.integration
def test_promote_re_gates_but_records_nothing_until_the_data_lands(quarantined):
    """Ledger 2026-09-26: the record must not run ahead of the data. promote() only re-gates;
    the row stays open and nothing is audited until mark_promoted() (after accept_promoted)."""
    v = promote(
        quarantined,
        actor=ACTOR,
        reason="checked the screenshots",
        rebuild=lambda k: first_heal_inputs(),
    )
    assert isinstance(v, ValidatedExtraction)
    assert _audits() == [] and _row(quarantined).resolved_as is None


@pytest.mark.integration
def test_mark_promoted_is_audited_and_resolves_the_row(quarantined):
    v = promote(
        quarantined,
        actor=ACTOR,
        reason="checked the screenshots",
        rebuild=lambda k: first_heal_inputs(),
    )
    mark_promoted(quarantined, actor=ACTOR, reason="checked the screenshots", verdicts=v.verdicts)
    (row,) = _audits()
    assert (row.actor, row.action, row.subject) == (ACTOR, "promote", RUN_KEY)
    assert row.detail["reason"] == "checked the screenshots"
    assert _row(quarantined).resolved_as == "promoted" and _row(quarantined).actor == ACTOR


@pytest.mark.integration
def test_a_refused_promotion_is_audited_and_leaves_the_row_open(quarantined):
    with pytest.raises(StillInvalid):
        promote(
            quarantined,
            actor=ACTOR,
            reason="looks fine",
            rebuild=lambda k: first_heal_inputs("12345678"),
        )
    (row,) = _audits()
    assert row.action == "promote_refused" and row.subject == RUN_KEY
    assert [f["check"] for f in row.detail["failing"]] == ["FormatAssertion"]
    assert _row(quarantined).resolved_as is None


@pytest.mark.integration
def test_discard_is_audited_and_resolves_the_row(quarantined):
    discard(quarantined, actor=ACTOR, reason="wrong column")
    (row,) = _audits()
    assert (row.action, row.subject, row.detail["reason"]) == ("discard", RUN_KEY, "wrong column")
    assert _row(quarantined).resolved_as == "discarded"


@pytest.mark.integration
def test_a_resolved_run_cannot_be_decided_twice(quarantined):
    discard(quarantined, actor=ACTOR, reason="wrong column")
    with pytest.raises(AlreadyResolved):
        promote(
            quarantined,
            actor=ACTOR,
            reason="changed my mind",
            rebuild=lambda k: first_heal_inputs(),
        )
    with pytest.raises(AlreadyResolved):
        discard(quarantined, actor=ACTOR, reason="again")


@pytest.mark.integration
def test_a_run_that_is_not_in_quarantine_is_refused(quarantined):
    with pytest.raises(NotInQuarantine):
        discard("testbench:2:1999-01-01", actor=ACTOR, reason="x")


@pytest.mark.integration
def test_two_concurrent_decisions_cannot_both_pass(quarantined):
    """Ruling 2: the open check reads FOR UPDATE. A holds the row lock; B's discard must block,
    then see A's committed decision and raise AlreadyResolved. Exactly one audit row."""
    outcome: dict[str, object] = {}
    with Session() as a:
        q = a.get(Quarantine, quarantined, with_for_update=True)
        assert q is not None and q.resolved_as is None

        def second() -> None:
            try:
                discard(quarantined, actor="b@example.invalid", reason="racing")
                outcome["b"] = "ok"
            except AlreadyResolved:
                outcome["b"] = "already"

        t = threading.Thread(target=second)
        t.start()
        time.sleep(1.0)
        assert t.is_alive(), "B did not block on the row lock"  # without FOR UPDATE it finishes
        q.resolved_as, q.actor = "discarded", "a@example.invalid"
        a.add(
            AuditLog(
                at=AT,
                actor="a@example.invalid",
                action="discard",
                subject=quarantined,
                detail={"reason": "first"},
            )
        )
        a.commit()
        t.join(timeout=10)
    assert outcome == {"b": "already"}
    assert len(_audits()) == 1


@pytest.mark.integration
def test_mark_promoted_after_discard_is_refused(quarantined):
    discard(quarantined, actor=ACTOR, reason="wrong column")
    with pytest.raises(AlreadyResolved):
        mark_promoted(quarantined, actor=ACTOR, reason="late", verdicts=[])
    assert len(_audits()) == 1


# ---- Task 26A fix round 3: compensation runs UNDER the row lock ------------------------------


def _fail_the_final_mark(monkeypatch):
    real = quarantine_mod._resolve_promoted

    def staged_then_boom(*a, **k):
        real(*a, **k)
        raise OSError("connection lost at commit")

    monkeypatch.setattr(quarantine_mod, "_resolve_promoted", staged_then_boom)


def _row_is_locked_by_someone_else(run_key: str) -> bool:
    """Probe from a second connection: can it take the row FOR UPDATE right now?"""
    from sqlalchemy.exc import OperationalError

    with Session() as probe:
        try:
            probe.execute(
                text("SELECT 1 FROM quarantine WHERE run_key = :k FOR UPDATE NOWAIT"),
                {"k": run_key},
            )
        except OperationalError:
            return True
        finally:
            probe.rollback()
    return False


@pytest.mark.integration
def test_the_compensation_runs_while_the_row_lock_is_still_held(quarantined, monkeypatch):
    """A failed final mark: the landed rows are taken back BEFORE the lock is released, so a
    racing discard cannot commit over them in between. The probe runs inside the compensation."""
    _fail_the_final_mark(monkeypatch)
    seen: dict[str, bool] = {}

    def compensate() -> None:
        seen["locked_during_compensation"] = _row_is_locked_by_someone_else(quarantined)

    with pytest.raises(OSError, match="connection lost"):
        quarantine_mod.promote_and_land(
            quarantined,
            actor=ACTOR,
            reason="checked",
            rebuild=lambda k: first_heal_inputs(),
            land=lambda v: quarantine_mod.Landed(rollback=compensate, finish=lambda: None),
        )
    assert seen == {"locked_during_compensation": True}
    assert not _row_is_locked_by_someone_else(quarantined)  # released afterwards
    row = _row(quarantined)
    assert row is not None and row.resolved_as is None  # still open and retryable
    assert _audits() == []


@pytest.mark.integration
def test_a_failing_session_rollback_does_not_skip_the_compensation(quarantined, monkeypatch):
    """If the session's own rollback raises, the landed rows are still taken back (compensation
    first), and the rollback failure surfaces loudly rather than being swallowed."""
    _fail_the_final_mark(monkeypatch)
    compensated: list[bool] = []

    def factory():
        s = Session()

        def broken_rollback() -> None:
            raise RuntimeError("rollback itself failed")

        s.rollback = broken_rollback
        return s

    with pytest.raises(RuntimeError, match="rollback itself failed"):
        quarantine_mod.promote_and_land(
            quarantined,
            actor=ACTOR,
            reason="checked",
            rebuild=lambda k: first_heal_inputs(),
            land=lambda v: quarantine_mod.Landed(
                rollback=lambda: compensated.append(True), finish=lambda: None
            ),
            session_factory=factory,
        )
    assert compensated == [True]
    row = _row(quarantined)
    assert row is not None and row.resolved_as is None


# ---- Task 26A follow-up: a compensation that itself fails ------------------------------------


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__context__
    return seen


@pytest.mark.integration
def test_a_failing_compensation_still_frees_the_lock_and_keeps_the_original_in_the_chain(
    quarantined, monkeypatch
):
    """A stranded landing is surfaced, not swallowed -- but it must not strand the row lock, and
    the failed mark that caused it stays readable as the exception's context."""
    _fail_the_final_mark(monkeypatch)

    def compensate() -> None:
        raise ValueError("undo failed")

    with pytest.raises(ValueError, match="undo failed") as caught:
        quarantine_mod.promote_and_land(
            quarantined,
            actor=ACTOR,
            reason="checked",
            rebuild=lambda k: first_heal_inputs(),
            land=lambda v: quarantine_mod.Landed(rollback=compensate, finish=lambda: None),
        )
    assert any(isinstance(e, OSError) for e in _chain(caught.value))
    assert not _row_is_locked_by_someone_else(quarantined)
    row = _row(quarantined)
    assert row is not None and row.resolved_as is None  # still open: nothing was marked
    assert _audits() == []


@pytest.mark.integration
def test_a_failing_compensation_and_a_failing_rollback_keep_both_causes_in_the_chain(
    quarantined, monkeypatch
):
    _fail_the_final_mark(monkeypatch)

    def factory():
        s = Session()

        def broken_rollback() -> None:
            raise RuntimeError("rollback itself failed")

        s.rollback = broken_rollback
        return s

    def compensate() -> None:
        raise ValueError("undo failed")

    with pytest.raises(RuntimeError, match="rollback itself failed") as caught:
        quarantine_mod.promote_and_land(
            quarantined,
            actor=ACTOR,
            reason="checked",
            rebuild=lambda k: first_heal_inputs(),
            land=lambda v: quarantine_mod.Landed(rollback=compensate, finish=lambda: None),
            session_factory=factory,
        )
    kinds = {type(e) for e in _chain(caught.value)}
    assert {RuntimeError, ValueError, OSError} <= kinds
