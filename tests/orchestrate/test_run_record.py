from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from selector_drift.db.models import AuditLog, Heartbeat, Run, RunAttempt, ScopeRecordRow
from selector_drift.db.session import Session
from selector_drift.orchestrate.flows import StartPageError, extraction_flow
from selector_drift.orchestrate.run_record import (
    NO_TERMINAL_STATE,
    heartbeat,
    record_attempt,
    run_key,
)
from selector_drift.spec.loader import load_spec
from selector_drift.spec.schema import SpecError

AlertCall = tuple[str, str, str | None, str]

REPO = Path(__file__).resolve().parents[2]
# A failing test must not sleep through two 300 s retries. The retry policy itself is pinned
# in test_deployments.py.
ONCE = extraction_flow.with_options(retries=0)
# Hookfix brief: the retry-attempt-identity tests below need one real retry (no sleep between
# attempts) so a Prefect retry actually re-enters extraction_flow in the same process/context --
# that re-entry is exactly what made a bare bool flag (`_ALERTED_IN_FLOW`) unsafe.
ONE_RETRY = extraction_flow.with_options(retries=1, retry_delay_seconds=0)


def _today() -> str:
    return dt.datetime.now(dt.UTC).date().isoformat()


def _bench_key() -> str:
    return run_key("testbench", load_spec(REPO / "specs" / "testbench.yaml").version, _today())


@pytest.mark.integration
def test_flow_writes_a_run_record(prefect_api, db, local_site) -> None:
    """Tightened from the first draft, which accepted `failed` as a pass."""
    key = ONCE("specs/testbench.yaml")
    with Session() as s:
        assert s.get(Run, key).state == "completed"
        (attempt,) = s.query(RunAttempt).filter_by(run_key=key).all()
        assert (attempt.attempt, attempt.state, attempt.error) == (1, "completed", None)
        assert s.query(Heartbeat).filter_by(run_key=key).count() == 1


@pytest.mark.unit
def test_run_key_is_deterministic() -> None:
    assert run_key("t", 1, "2026-09-07") == run_key("t", 1, "2026-09-07")


@pytest.mark.integration
def test_a_refused_target_never_opens_a_browser(prefect_api, db, expired_spec, monkeypatch) -> None:
    opened: list[int] = []
    monkeypatch.setattr("selector_drift.browser.driver.open", lambda *a, **k: opened.append(1))
    key = ONCE(expired_spec)
    with Session() as s:
        run = s.get(Run, key)
        assert run.state == "refused" and "expired" in (run.error or "")
        # Task 34B's fired_without_attempt depends on this absence.
        assert s.query(RunAttempt).filter_by(run_key=key).count() == 0
    assert opened == []


@pytest.mark.integration
def test_every_fire_writes_a_heartbeat_even_when_refused(prefect_api, db, expired_spec) -> None:
    key = ONCE(expired_spec)
    with Session() as s:
        hb = s.query(Heartbeat).filter_by(run_key=key).one()
    assert hb.target == "expired"
    assert hb.fired_at >= hb.scheduled_for.replace(second=0, microsecond=0)


@pytest.mark.integration
def test_the_heartbeat_is_written_before_authorisation(
    prefect_api, db, local_site, monkeypatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("scope gate unreachable")

    monkeypatch.setattr("selector_drift.orchestrate.flows.authorise", boom)
    with pytest.raises(RuntimeError, match="unreachable"):
        ONCE("specs/testbench.yaml")
    with Session() as s:
        hb = s.query(Heartbeat).filter_by(target="testbench").one()
        assert hb.run_key == _bench_key()
        assert s.get(Run, _bench_key()) is None  # no runs row, no attempt row


@pytest.mark.integration
def test_an_unloadable_spec_still_leaves_a_heartbeat(prefect_api, db, tmp_path) -> None:
    bad = tmp_path / "ghost.yaml"
    bad.write_text("version: [not, an, int]\n", encoding="utf-8")
    with pytest.raises(SpecError):
        ONCE(str(bad))
    with Session() as s:
        hb = s.query(Heartbeat).filter_by(target="ghost").one()
    assert hb.run_key is None


@pytest.mark.integration
def test_an_unknown_target_is_refused_not_failed(prefect_api, db, tmp_path, scope_dir) -> None:
    """Design 4.3: no scope record -> Refuse, with a runs row the log can count."""
    specs = tmp_path / "specs"
    specs.mkdir()
    body = (REPO / "specs" / "testbench.yaml").read_text(encoding="utf-8")
    (specs / "nowhere.yaml").write_text(
        body.replace("scopes/testbench.yaml", "scopes/nowhere.yaml"), encoding="utf-8"
    )
    key = ONCE(str(specs / "nowhere.yaml"))
    with Session() as s:
        run = s.get(Run, key)
        assert run.state == "refused" and "unknown target" in (run.error or "")


@pytest.mark.integration
def test_a_page_that_will_not_load_is_a_failed_attempt_and_the_flow_raises(
    prefect_api, db, dead_site
) -> None:
    """Design 9.3 Fail: marked failed, alerted, and raised so Prefect's retry policy applies."""
    # The exception type is Task 4's; any failure that carries a reason will do.
    with pytest.raises(Exception, match=r"\S"):
        ONCE("specs/testbench.yaml")
    with Session() as s:
        run = s.get(Run, _bench_key())
        assert run.state == "failed" and run.error and run.error != NO_TERMINAL_STATE
        (attempt,) = s.query(RunAttempt).filter_by(run_key=_bench_key()).all()
        assert attempt.state == "failed" and attempt.finished_at is not None


@pytest.mark.integration
def test_a_second_run_in_the_same_window_is_a_second_attempt_not_an_overwrite(
    prefect_api, db, local_site
) -> None:
    """M13. A retry must not erase the attempt it retried."""
    first = ONCE("specs/testbench.yaml")
    second = ONCE("specs/testbench.yaml")
    assert first == second
    with Session() as s:
        attempts = s.query(RunAttempt).filter_by(run_key=first).order_by(RunAttempt.attempt).all()
    assert [(a.attempt, a.state) for a in attempts] == [(1, "completed"), (2, "completed")]


@pytest.mark.integration
def test_a_crash_after_start_leaves_a_failed_run_not_a_missing_one(db) -> None:
    key = "testbench:1:2026-09-07"
    record_attempt(key, 1, "started", None, "0" * 64)
    with Session() as s:
        run = s.get(Run, key)
        assert (run.state, run.error) == ("failed", NO_TERMINAL_STATE)
        assert run.window == dt.date(2026, 9, 7) and run.spec_version == 1
        assert s.query(RunAttempt).filter_by(run_key=key).one().finished_at is None


@pytest.mark.integration
def test_a_retry_of_the_same_slot_does_not_double_count_its_heartbeat(db) -> None:
    slot = dt.datetime(2026, 9, 7, 4, 12, tzinfo=dt.UTC)
    first = heartbeat("testbench", slot, None)
    assert heartbeat("testbench", slot, None) == first
    with Session() as s:
        assert s.query(Heartbeat).count() == 1


@pytest.mark.integration
def test_every_authorised_run_audits_the_scope_record_hash(prefect_api, db, local_site) -> None:
    """Task 2: which authorisation was in force is answerable from the database."""
    key = ONCE("specs/testbench.yaml")
    with Session() as s:
        row = s.query(AuditLog).filter_by(action="scope_authorised", subject=key).one()
        attempt = s.query(RunAttempt).filter_by(run_key=key).one()
    assert row.detail["record_sha256"] == attempt.scope_record_sha256
    assert row.detail["target"] == "testbench"


@pytest.mark.integration
def test_every_authorised_run_mirrors_its_scope_record(prefect_api, db, local_site) -> None:
    """Found 2026-09-26: nothing wrote scope_records, which the targets screen and the retention
    job read. The mirror carries the hash the audit row carries."""
    key = ONCE("specs/testbench.yaml")
    with Session() as s:
        mirror = s.get(ScopeRecordRow, "testbench")
        attempt = s.query(RunAttempt).filter_by(run_key=key).one()
    assert mirror is not None and mirror.record_sha256 == attempt.scope_record_sha256
    assert mirror.warmup_runs >= 1 and mirror.loaded_at is not None
    assert mirror.credential_posture == "session_injection"


@pytest.mark.integration
def test_a_spec_whose_scope_ref_names_another_target_is_refused(
    prefect_api, db, mismatch_spec, monkeypatch
) -> None:
    """I-1 (final review): the stem-mismatch refusal branch (flows.py) is the only thing that
    stops a spec for one target running under another target's authorisation -- CLAUDE.md rule 1.
    `mismatch_spec` is named "mismatch" but its scope_ref names "testbench"."""
    opened: list[int] = []
    monkeypatch.setattr("selector_drift.browser.driver.open", lambda *a, **k: opened.append(1))
    key = ONCE(mismatch_spec)
    with Session() as s:
        run = s.get(Run, key)
        assert run.state == "refused"
        assert "testbench" in (run.error or "")
        assert s.query(RunAttempt).filter_by(run_key=key).count() == 0
    assert opened == []


@pytest.mark.integration
def test_a_refusal_posts_an_alert(prefect_api, db, expired_spec, monkeypatch) -> None:
    """M-5 (final review): a later rewrite that drops the post_alert call at the refusal branch
    must go red here, not silently alert nobody for 90 days."""
    calls: list[AlertCall] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )
    key = ONCE(expired_spec)
    assert len(calls) == 1
    kind, target, alerted_key, _summary = calls[0]
    assert (kind, target, alerted_key) == ("refused", "expired", key)


@pytest.mark.integration
def test_a_failure_posts_an_alert(prefect_api, db, dead_site, monkeypatch) -> None:
    """M-5 (final review): same as above, for the post-authorisation failure branch."""
    calls: list[AlertCall] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )
    with pytest.raises(Exception, match=r"\S"):
        ONCE("specs/testbench.yaml")
    assert len(calls) == 1
    kind, target, alerted_key, _summary = calls[0]
    assert (kind, target, alerted_key) == ("failed", "testbench", _bench_key())


@pytest.mark.integration
def test_the_failure_alert_never_carries_page_content(
    prefect_api, db, local_site, monkeypatch
) -> None:
    """I-2 (final review): a Playwright locator timeout's "Call log:" section quotes the DOM
    content it was matching against -- here, a claim number cell. That must never reach the
    third-party alert webhook (CLAUDE.md rule 6), only run_attempts.error / runs.error."""

    class BoomError(RuntimeError):
        pass

    def boom(*a: object, **k: object) -> None:
        raise BoomError(
            "Timeout 5000ms exceeded.\nCall log:\n  - locator resolved to <td>AB12345678</td>\n"
        )

    monkeypatch.setattr("selector_drift.browser.driver.open", boom)
    calls: list[AlertCall] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )
    with pytest.raises(BoomError):
        ONCE("specs/testbench.yaml")
    assert len(calls) == 1
    _kind, _target, _key, summary = calls[0]
    assert "<td" not in summary
    assert "AB12345678" not in summary
    assert "Call log" not in summary
    assert "BoomError" in summary
    # The full text is still recorded inside the boundary.
    with Session() as s:
        run = s.get(Run, _bench_key())
        assert run is not None and run.error is not None and "<td" in run.error


@pytest.mark.integration
def test_fail_post_auth_then_fail_pre_auth_alerts_exactly_twice(
    prefect_api, db, local_site, monkeypatch
) -> None:
    """Hookfix brief, regression test A. Attempt 1 fails post-authorisation (the flow body's own
    `except Exception` branch posts an alert and records its run_count before raising). Attempt 2
    (the one retry) fails pre-authorisation -- a SpecError loading the spec, before the flow body
    ever reaches its own alert branch again. `alert_on_flow_failure` (on_failure) must still
    alert for attempt 2, because the value it must compare against is the run_count of the
    attempt that actually failed last, not a flag that merely remembers some earlier attempt
    alerted. On e218ab0, `_ALERTED_IN_FLOW` is a bare bool that stays True across the retry, so
    the hook wrongly skips attempt 2's alert -- this must go red there."""
    calls: list[AlertCall] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )

    real_load_spec = load_spec
    spec_calls = {"n": 0}

    def flaky_load_spec(path: Path):  # type: ignore[no-untyped-def]
        spec_calls["n"] += 1
        if spec_calls["n"] >= 2:
            raise SpecError("synthetic pre-auth failure on the retry attempt")
        return real_load_spec(path)

    async def always_fail_start_page(target: object) -> int:
        raise StartPageError("synthetic post-auth failure on the first attempt")

    monkeypatch.setattr("selector_drift.orchestrate.flows.load_spec", flaky_load_spec)
    monkeypatch.setattr("selector_drift.orchestrate.flows.load_start_page", always_fail_start_page)

    with pytest.raises(Exception, match=r"\S"):
        ONE_RETRY("specs/testbench.yaml")

    assert len(calls) == 2
    # calls[0] is attempt 1's in-body alert: it carries the real run_key. calls[1] is the hook's
    # alert for attempt 2 -- the hook never learns the run_key (M-5/plus item, unchanged by this
    # brief), and its summary is built from the SpecError the retry raised, proving this alert
    # came from the hook, not a second in-body post.
    assert calls[0] == (
        "failed",
        "testbench",
        _bench_key(),
        "StartPageError: synthetic post-auth failure on the first attempt",
    )
    kind, target, alerted_key, summary = calls[1]
    assert (kind, target, alerted_key) == ("failed", "testbench", None)
    assert "SpecError" in summary


@pytest.mark.integration
def test_fail_post_auth_then_succeed_alerts_exactly_once(
    prefect_api, db, local_site, monkeypatch
) -> None:
    """Hookfix brief, regression test B. Attempt 1 fails post-authorisation and alerts in-body.
    Attempt 2 (the retry) succeeds. The flow completes, so the on_failure hook never runs at
    all -- exactly one alert, from attempt 1's own branch."""
    calls: list[AlertCall] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )

    start_calls = {"n": 0}

    async def fail_then_succeed(target: object) -> int:
        start_calls["n"] += 1
        if start_calls["n"] == 1:
            raise StartPageError("synthetic post-auth failure on the first attempt")
        return 42

    monkeypatch.setattr("selector_drift.orchestrate.flows.load_start_page", fail_then_succeed)

    key = ONE_RETRY("specs/testbench.yaml")

    assert len(calls) == 1
    assert calls[0][0] == "failed"
    with Session() as s:
        assert s.get(Run, key).state == "completed"


@pytest.mark.integration
def test_fail_post_auth_then_fail_post_auth_alerts_exactly_twice_hook_skips(
    prefect_api, db, local_site, monkeypatch
) -> None:
    """Hookfix brief, regression test C. Both attempts fail post-authorisation, each posting its
    own in-body alert and recording its own run and attempt identity. When the hook fires after
    the final attempt, the recorded identity matches the run and attempt that actually failed
    last, so the hook must skip -- exactly two alerts total, both in-body, no third from the
    hook."""
    calls: list[AlertCall] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )

    async def always_fail_start_page(target: object) -> int:
        raise StartPageError("synthetic post-auth failure on every attempt")

    monkeypatch.setattr("selector_drift.orchestrate.flows.load_start_page", always_fail_start_page)

    with pytest.raises(Exception, match=r"\S"):
        ONE_RETRY("specs/testbench.yaml")

    assert len(calls) == 2
    kinds = [c[0] for c in calls]
    assert kinds == ["failed", "failed"]
    # Both alerts carry the real run_key -- proof both came from the flow body's own
    # post-authorisation branch, never from the hook (which never learns the run_key).
    key = _bench_key()
    assert [c[2] for c in calls] == [key, key]


@pytest.mark.integration
def test_a_second_flow_run_is_not_suppressed_by_the_first_runs_alert_flag(
    prefect_api, db, local_site, monkeypatch
) -> None:
    """Hookfix brief, round 1, Important finding: Prefect 3.8.4 runs a *sync* flow body in the
    caller's context with no `copy_context()` (flow_engine.py: call_with_parameters), so
    `_ALERTED_IN_FLOW` is a ContextVar shared across sequential in-process flow runs, not scoped
    to one flow run. Keying the flag on `run_count` alone is not enough: every flow run's single
    (here, un-retried) attempt is run_count 1, so a value left behind by one flow run's in-body
    alert can equal a *later, unrelated* flow run's `flow_run.run_count` and wrongly suppress
    that later run's hook alert -- even though that later run's only attempt never reached the
    in-body alert branch at all. This must go red if the flag is keyed on run_count alone (it did,
    against the round-1-reviewed code: the reviewer's own probe showed a second in-process run's
    hook seeing a leaked value and skipping).

    Two sequential ONCE (no-retry) flow runs, same process, same event loop:
    - Run 1 fails post-authorisation (StartPageError) -- alerts in-body, records
      (run_1's flow_run.id, 1).
    - Run 2 fails pre-authorisation (SpecError, before the in-body alert branch) -- run_count is
      *also* 1 (a fresh flow run, not a retry), but its flow_run.id is different. The hook must
      still alert for run 2: the stored identity from run 1 must not match run 2's."""
    calls: list[AlertCall] = []
    monkeypatch.setattr(
        "selector_drift.orchestrate.flows.post_alert",
        lambda kind, target, run_key, summary: calls.append((kind, target, run_key, summary)),
    )

    async def always_fail_start_page(target: object) -> int:
        raise StartPageError("synthetic post-auth failure, run 1")

    real_load_spec = load_spec
    stage = {"n": 1}

    def routed_load_spec(path: Path):  # type: ignore[no-untyped-def]
        if stage["n"] == 2:
            raise SpecError("synthetic pre-auth failure, run 2")
        return real_load_spec(path)

    monkeypatch.setattr("selector_drift.orchestrate.flows.load_start_page", always_fail_start_page)
    monkeypatch.setattr("selector_drift.orchestrate.flows.load_spec", routed_load_spec)

    # Run 1: fails post-auth, alerts in-body, hook correctly skips (only 1 alert).
    with pytest.raises(StartPageError):
        ONCE("specs/testbench.yaml")
    assert len(calls) == 1
    assert calls[0][0] == "failed" and calls[0][2] == _bench_key()

    # Run 2: a genuinely new flow run (new flow_run.id) whose only attempt is also run_count 1,
    # but fails pre-auth -- it never touches _ALERTED_IN_FLOW itself. If the leaked value from
    # run 1 is compared by run_count alone, the hook wrongly thinks *this* attempt already
    # alerted and stays silent -- silent alert loss for an entirely different run.
    calls.clear()
    stage["n"] = 2
    with pytest.raises(SpecError):
        ONCE("specs/testbench.yaml")
    assert len(calls) == 1
    kind, target, alerted_key, summary = calls[0]
    assert (kind, target, alerted_key) == ("failed", "testbench", None)
    assert "SpecError" in summary
