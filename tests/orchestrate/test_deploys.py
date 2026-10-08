"""Task 29A: deploy history for the control arm (M10). No real network: GitHub is mocked.

Adapted from the brief: SD_DEPLOYS_FILE and SD_GITHUB_TOKEN are read through Settings (one place
reads configuration, 2026-09-26), so the Settings cache is cleared around every test."""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from selector_drift.config import get_settings
from selector_drift.db.models import Deploy, HealEvent, Run
from selector_drift.db.session import Session, engine
from selector_drift.orchestrate.deploys import (
    GitHubDeployments,
    SelfHostLog,
    feed_for,
    ingest_deploys,
    preceding_deploy,
)

GH = "https://api.github.com/repos/owner/app/deployments?environment=production&per_page=100"


def at(day: int, h: int = 4, m: int = 12) -> dt.datetime:
    return dt.datetime(2026, 10, day, h, m, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def clean(request: pytest.FixtureRequest) -> Iterator[None]:
    get_settings.cache_clear()
    if request.node.get_closest_marker("integration") is not None:
        with engine.begin() as c:
            c.execute(text("TRUNCATE deploys, heal_events, runs RESTART IDENTITY CASCADE"))
    yield
    get_settings.cache_clear()


def _log(tmp_path: Path, *lines: dict[str, str]) -> SelfHostLog:
    p = tmp_path / "deploys.jsonl"
    p.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return SelfHostLog(p)


def _heal_after_two_runs() -> HealEvent:
    with Session() as s:
        s.add(
            Run(
                run_key="testbench:2:2026-10-01",
                target="testbench",
                spec_version=2,
                window=at(1).date(),
                first_started_at=at(1),
                state="completed",
            )
        )
        s.add(
            Run(
                run_key="testbench:2:2026-10-02",
                target="testbench",
                spec_version=2,
                window=at(2).date(),
                first_started_at=at(2),
                state="healed",
            )
        )
        s.flush()
        heal = HealEvent(
            run_key="testbench:2:2026-10-02",
            target="testbench",
            field="amount",
            broke_at=at(2, 4, 13),
            winning_rung=3,
            selector_after="td",
            anchor_after={},
            trace=[],
            verdicts=[],
        )
        s.add(heal)
        s.commit()
        s.refresh(heal)
    return heal


@pytest.mark.integration
def test_self_host_deploys_are_ingested_idempotently(tmp_path) -> None:
    feed = _log(
        tmp_path,
        {"deployed_at": at(2, 1).isoformat(), "ref": "a1"},
        {"deployed_at": at(3, 1).isoformat(), "ref": "b2"},
    )
    assert ingest_deploys("testbench", feed=feed) == 2
    assert ingest_deploys("testbench", feed=feed) == 0
    with Session() as s:
        assert {d.source for d in s.query(Deploy)} == {"self-host"}
        assert s.query(Deploy).count() == 2


@pytest.mark.integration
def test_a_duplicate_line_in_one_feed_is_one_deploy(tmp_path) -> None:
    line = {"deployed_at": at(2, 1).isoformat(), "ref": "a1"}
    assert ingest_deploys("testbench", feed=_log(tmp_path, line, line)) == 1


@pytest.mark.integration
def test_the_same_instant_in_another_offset_is_the_same_deploy(tmp_path) -> None:
    utc = at(2, 1)
    plus2 = utc.astimezone(dt.timezone(dt.timedelta(hours=2)))
    assert (
        ingest_deploys(
            "testbench", feed=_log(tmp_path, {"deployed_at": utc.isoformat(), "ref": "a1"})
        )
        == 1
    )
    p = tmp_path / "other.jsonl"
    p.write_text(
        json.dumps({"deployed_at": plus2.isoformat(), "ref": "a1"}) + "\n", encoding="utf-8"
    )
    assert ingest_deploys("testbench", feed=SelfHostLog(p)) == 0


@pytest.mark.integration
def test_a_deploy_time_without_a_timezone_is_refused(tmp_path) -> None:
    feed = _log(tmp_path, {"deployed_at": "2026-10-02T01:00:00", "ref": "a1"})
    with pytest.raises(ValueError, match="timezone"):
        ingest_deploys("testbench", feed=feed)


@pytest.mark.integration
def test_github_deployments_are_read_with_the_token_in_a_header_only(httpx_mock, caplog) -> None:
    token = SecretStr("gh-test-only-not-a-secret")
    httpx_mock.add_response(url=GH, json=[{"created_at": "2026-10-02T01:00:00Z", "sha": "c3"}])
    with caplog.at_level(logging.DEBUG):
        n = ingest_deploys("testbench", feed=GitHubDeployments("owner/app", token=token))
    assert n == 1
    (request,) = httpx_mock.get_requests()
    assert request.headers["Authorization"] == "Bearer gh-test-only-not-a-secret"
    assert token.get_secret_value() not in str(request.url)
    assert token.get_secret_value() not in caplog.text
    with Session() as s:
        (d,) = s.query(Deploy)
        assert (d.source, d.ref, d.deployed_at) == ("gh-actions", "c3", at(2, 1, 0))


@pytest.mark.unit
def test_github_pagination_is_followed(httpx_mock) -> None:
    page2 = "https://api.github.com/repositories/1/deployments?environment=production&per_page=100&page=2"
    httpx_mock.add_response(
        url=GH,
        json=[{"created_at": "2026-10-02T01:00:00Z", "sha": "c3"}],
        headers={"Link": f'<{page2}>; rel="next"'},
    )
    httpx_mock.add_response(url=page2, json=[{"created_at": "2026-10-01T01:00:00Z", "sha": "b2"}])
    records = GitHubDeployments("owner/app").fetch()
    assert [r.ref for r in records] == ["c3", "b2"]


@pytest.mark.unit
def test_the_token_never_follows_a_link_off_the_github_api(httpx_mock) -> None:
    httpx_mock.add_response(
        url=GH,
        json=[],
        headers={"Link": '<https://evil.example.invalid/steal?page=2>; rel="next"'},
    )
    token = SecretStr("gh-test-only-not-a-secret")
    with pytest.raises(ValueError, match=r"api\.github\.com"):
        GitHubDeployments("owner/app", token=token).fetch()
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.unit
def test_a_github_error_is_raised_without_the_token(httpx_mock) -> None:
    httpx_mock.add_response(url=GH, status_code=401)
    token = SecretStr("gh-test-only-not-a-secret")
    with pytest.raises(Exception) as caught:
        GitHubDeployments("owner/app", token=token).fetch()
    assert token.get_secret_value() not in str(caught.value)


@pytest.mark.integration
def test_a_heal_on_a_control_target_is_joinable_to_a_deploy(tmp_path) -> None:
    # The control arm's whole claim (design 8.1).
    heal = _heal_after_two_runs()
    ingest_deploys(
        "testbench", feed=_log(tmp_path, {"deployed_at": at(2, 1).isoformat(), "ref": "a1"})
    )
    found = preceding_deploy(heal)
    assert found is not None and found.ref == "a1"


@pytest.mark.integration
def test_a_heal_with_no_deploy_since_the_last_run_has_none(tmp_path) -> None:
    heal = _heal_after_two_runs()
    ingest_deploys(
        "testbench", feed=_log(tmp_path, {"deployed_at": at(1, 1).isoformat(), "ref": "old"})
    )  # before the last run
    assert preceding_deploy(heal) is None


@pytest.mark.integration
def test_another_targets_deploy_does_not_count(tmp_path) -> None:
    heal = _heal_after_two_runs()
    ingest_deploys(
        "elsewhere", feed=_log(tmp_path, {"deployed_at": at(2, 1).isoformat(), "ref": "x"})
    )
    assert preceding_deploy(heal) is None


@pytest.mark.unit
def test_a_target_with_no_declared_feed_is_refused(tmp_path, monkeypatch) -> None:
    f = tmp_path / "deploys.yaml"
    f.write_text("testbench: { source: self-host, path: deploy/deploys.jsonl }\n", encoding="utf-8")
    monkeypatch.setenv("SD_DEPLOYS_FILE", str(f))
    get_settings.cache_clear()
    assert isinstance(feed_for("testbench"), SelfHostLog)
    with pytest.raises(LookupError, match="control-app"):
        feed_for("control-app")


@pytest.mark.unit
def test_a_gh_actions_feed_takes_its_token_from_settings(tmp_path, monkeypatch) -> None:
    f = tmp_path / "deploys.yaml"
    f.write_text(
        "app: { source: gh-actions, repo: owner/app, environment: prod }\n", encoding="utf-8"
    )
    monkeypatch.setenv("SD_DEPLOYS_FILE", str(f))
    monkeypatch.setenv("SD_GITHUB_TOKEN", "gh-test-only-not-a-secret")
    get_settings.cache_clear()
    feed = feed_for("app")
    assert isinstance(feed, GitHubDeployments)
    assert (feed.repo, feed.environment, feed.source) == ("owner/app", "prod", "gh-actions")
    assert feed.token is not None and feed.token.get_secret_value() == "gh-test-only-not-a-secret"
    assert "gh-test-only" not in repr(feed) and "gh-test-only" not in repr(get_settings())


@pytest.mark.unit
def test_an_unknown_source_is_refused(tmp_path, monkeypatch) -> None:
    f = tmp_path / "deploys.yaml"
    f.write_text("app: { source: ftp, path: x }\n", encoding="utf-8")
    monkeypatch.setenv("SD_DEPLOYS_FILE", str(f))
    get_settings.cache_clear()
    with pytest.raises(LookupError, match="ftp"):
        feed_for("app")


# ------------------------------------------------------------------ fix round 1

WEBHOOK = "https://hooks.example.invalid/services/T0/B0/test-only-not-a-secret"
REPO = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_a_relative_feed_path_resolves_against_the_deploys_file_not_the_cwd(
    tmp_path, monkeypatch
) -> None:
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "deploys.yaml").write_text(
        "a: { source: self-host, path: logs/a.jsonl }\n"
        "b: { source: self-host, path: /data/deploys/b.jsonl }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SD_DEPLOYS_FILE", str(conf / "deploys.yaml"))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    a = feed_for("a")
    assert isinstance(a, SelfHostLog) and a.path == conf / "logs" / "a.jsonl"
    b = feed_for("b")
    assert isinstance(b, SelfHostLog) and b.path.as_posix().endswith("/data/deploys/b.jsonl")
    assert not b.path.as_posix().startswith(conf.as_posix())


@pytest.mark.unit
def test_shipped_compose_and_deploys_file_put_the_self_host_log_on_a_writable_mount() -> None:
    import yaml

    from selector_drift.orchestrate.deploys import resolve_feed_path

    worker = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"][
        "worker"
    ]
    deploys_file = PurePosixPath(worker["environment"]["SD_DEPLOYS_FILE"])
    declared = yaml.safe_load((REPO / "deploys.yaml").read_text(encoding="utf-8"))
    mounts = []
    for v in worker["volumes"]:
        parts = str(v).split(":")
        if len(parts) == 2 or (len(parts) == 3 and "ro" not in parts[2].split(",")):
            mounts.append(PurePosixPath(parts[1]))
    assert mounts, "the worker has no writable volume"
    for target, entry in declared.items():
        if entry["source"] != "self-host":
            continue
        log = resolve_feed_path(str(entry["path"]), deploys_file)
        assert isinstance(log, PurePosixPath) and log.is_absolute(), target
        assert any(log.is_relative_to(m) for m in mounts), (target, log, mounts)
        assert not log.is_relative_to(PurePosixPath("/app")), "the rootfs is read-only"


@pytest.mark.integration
def test_a_missing_self_host_log_is_a_condition_alerted_once_a_day(
    tmp_path, monkeypatch, httpx_mock
) -> None:
    monkeypatch.setenv("SD_ALERT_WEBHOOK_URL", WEBHOOK)
    get_settings.cache_clear()
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit_log RESTART IDENTITY CASCADE"))
    httpx_mock.add_response(url=WEBHOOK, method="POST")
    feed = SelfHostLog(tmp_path / "never-written.jsonl")
    assert feed.fetch() == []
    assert ingest_deploys("testbench", feed=feed, now=at(2)) == 0
    assert ingest_deploys("testbench", feed=feed, now=at(2, 4, 27)) == 0  # same UTC day
    (request,) = httpx_mock.get_requests()
    body = json.loads(request.content)
    assert (body["target"], body["rule"]) == ("testbench", "deploy_feed_missing")
    assert set(body) == {"target", "rule", "severity", "title", "run_key", "threshold", "day"}
    assert "never-written" not in request.content.decode()


@pytest.mark.integration
def test_a_corrupt_self_host_log_still_raises(tmp_path) -> None:
    p = tmp_path / "deploys.jsonl"
    p.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        ingest_deploys("testbench", feed=SelfHostLog(p))


@pytest.mark.unit
def test_the_shipped_deploys_file_declares_the_bench() -> None:
    import yaml

    declared = yaml.safe_load((REPO / "deploys.yaml").read_text(encoding="utf-8"))
    assert declared == {
        "testbench": {"source": "self-host", "path": "/data/deploys/testbench.jsonl"}
    }
