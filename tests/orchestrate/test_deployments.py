from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from selector_drift.config import get_settings
from selector_drift.orchestrate.deployments import JOB_LIMITS, extraction_deployments
from selector_drift.orchestrate.flows import extraction_flow
from selector_drift.spec.loader import load_spec

REPO = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


@pytest.fixture(autouse=True)
def repo_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(REPO)
    monkeypatch.delenv("SD_SPEC_DIR", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.unit
def test_one_deployment_per_spec_on_its_own_cron_in_utc() -> None:
    deployments = {d.name: d for d in extraction_deployments()}
    specs = sorted((REPO / "specs").glob("*.yaml"))
    assert set(deployments) == {f"extract-{p.stem}" for p in specs}
    for p in specs:
        d = deployments[f"extract-{p.stem}"]
        (schedule,) = d.schedules
        assert schedule.schedule.cron == load_spec(p).schedule
        assert schedule.schedule.timezone == "UTC"  # M20
        assert d.parameters == {"spec_path": p.name}


@pytest.mark.unit
def test_the_flow_has_the_ten_minute_timeout_and_bounded_retries() -> None:
    """B8: a hard timeout at the deployment level; two retries, five minutes apart."""
    assert extraction_flow.timeout_seconds == 600
    assert extraction_flow.retries == 2 and extraction_flow.retry_delay_seconds == 300


@pytest.mark.unit
def test_the_container_enforces_the_limits_the_deployment_declares() -> None:
    worker = COMPOSE["worker"]
    assert worker["mem_limit"] == JOB_LIMITS["mem_limit"]
    assert worker["pids_limit"] == JOB_LIMITS["pids_limit"]
    assert all(d.job_variables == JOB_LIMITS for d in extraction_deployments())
    assert worker["init"] is True  # reaps zombie Chromiums (B8)
    # M-6 (final review): "seccomp=unconfined" is one of two Docker-accepted spellings;
    # "seccomp:unconfined" also disables the seccomp profile and must be rejected too.
    assert not any("unconfined" in opt for opt in worker["security_opt"])  # H1


@pytest.mark.unit
def test_the_scheduler_survives_a_restart_and_runs_in_utc() -> None:
    """B3/B8: real orchestrator persistence, never a `while true` loop."""
    worker, server = COMPOSE["worker"], COMPOSE["prefect-server"]
    assert worker["restart"] == server["restart"] == "unless-stopped"
    assert worker["command"] == ["python", "-m", "selector_drift.orchestrate.deployments"]
    assert worker["environment"]["TZ"] == "UTC"
    assert worker["environment"]["PREFECT_API_URL"] == "http://prefect-server:4200/api"
    assert "postgresql+asyncpg://" in server["environment"]["PREFECT_API_DATABASE_CONNECTION_URL"]


@pytest.mark.unit
def test_every_deployment_is_tagged_with_its_target() -> None:
    for d in extraction_deployments():
        assert d.tags == ["extraction", d.name.removeprefix("extract-")]
