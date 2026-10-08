"""Task 29A: the fifteen-minute watch flow and its deployment. Unit-level: the flow body runs via
`.fn` with its collaborators replaced, so no Prefect server and no database (M26)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from selector_drift.config import get_settings
from selector_drift.orchestrate import watch
from selector_drift.orchestrate.deployments import WATCH_CRON, watch_deployment


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    f = tmp_path / "deploys.yaml"
    f.write_text(
        "alpha: { source: self-host, path: a.jsonl }\n"
        "beta: { source: self-host, path: b.jsonl }\n"
        "gamma: { source: self-host, path: c.jsonl }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SD_DEPLOYS_FILE", str(f))
    get_settings.cache_clear()
    seen: list[str] = []
    monkeypatch.setattr(watch, "check_alert_channel", lambda: seen.append("channel") or True)
    monkeypatch.setattr(watch, "notify_missed_heartbeats", lambda: seen.append("heartbeats"))
    monkeypatch.setattr(watch, "notify_false_accepts", lambda: seen.append("false_accepts"))
    yield seen
    get_settings.cache_clear()


@pytest.mark.unit
def test_the_watch_runs_every_check_and_ingests_every_declared_target(calls, monkeypatch) -> None:
    monkeypatch.setattr(watch, "ingest_deploys", lambda target: calls.append(target) or 0)
    watch.alert_watch.fn()
    assert calls == ["channel", "heartbeats", "false_accepts", "alpha", "beta", "gamma"]


@pytest.mark.unit
def test_one_feed_down_does_not_stop_the_others_and_fails_the_run(calls, monkeypatch) -> None:
    def ingest(target: str) -> int:
        calls.append(target)
        if target == "beta":
            raise FileNotFoundError("b.jsonl")
        return 0

    monkeypatch.setattr(watch, "ingest_deploys", ingest)
    with pytest.raises(RuntimeError, match="beta"):
        watch.alert_watch.fn()
    assert calls[-3:] == ["alpha", "beta", "gamma"]


@pytest.mark.unit
def test_the_watch_is_deployed_every_fifteen_minutes() -> None:
    d = watch_deployment()
    assert WATCH_CRON == "*/15 * * * *"
    assert d.name == "alert-watch"
    (schedule,) = d.schedules
    assert schedule.schedule.cron == WATCH_CRON
    assert schedule.schedule.timezone == "UTC"
    assert d.job_variables == {"mem_limit": "256m"}
    assert watch.alert_watch.timeout_seconds == 300 and watch.alert_watch.retries == 0
