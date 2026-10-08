"""Deploy history for the control arm (M10). 'Heals fired only on runs following an actual
deploy, verified against deploy history' is checkable only against a stored feed.

Each target's feed is declared in `deploys.yaml` (path: Settings.deploys_file, SD_DEPLOYS_FILE):
`self-host` -- the VM's deploy script appends one JSON line per deploy -- or `gh-actions` --
GitHub's deployments API. Ingest is idempotent on (target, deployed_at, ref), the deploys
table's primary key. A relative self-host `path` resolves against deploys.yaml's own directory,
never the process cwd. A self-host log that does not exist yet is a condition, not a crash: no
records, and one deduplicated `deploy_feed_missing` alert per target per UTC day (fix round 1);
a log that exists but cannot be read or parsed still raises. The GitHub token
(Settings.github_token) travels in a header only, never
in a URL, a log line or an exception message.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from selector_drift.config import get_settings
from selector_drift.db.models import Deploy, HealEvent, Run
from selector_drift.db.session import Session
from selector_drift.quality.alerts import notify_deploy_feed_missing

Source = Literal["self-host", "gh-actions", "manual"]
GITHUB_API: Final = "https://api.github.com"
GITHUB_TIMEOUT_SECONDS: Final = 15.0


@dataclass(frozen=True)
class DeployRecord:
    deployed_at: dt.datetime
    ref: str


class DeployFeed(Protocol):
    @property
    def source(self) -> Source: ...

    def fetch(self) -> list[DeployRecord]: ...


def _aware(value: str, where: str) -> dt.datetime:
    at = dt.datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError(f"{where}: deployed_at has no timezone; the log is UTC throughout (M20)")
    return at.astimezone(dt.UTC)


@dataclass(frozen=True)
class SelfHostLog:
    path: Path
    source: Source = "self-host"

    @property
    def missing(self) -> bool:
        return not self.path.exists()

    def fetch(self) -> list[DeployRecord]:
        if self.missing:
            return []  # nothing deployed through this feed yet; ingest_deploys alerts on it
        out: list[DeployRecord] = []
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip():
                obj = json.loads(line)
                out.append(
                    DeployRecord(
                        _aware(str(obj["deployed_at"]), f"{self.path}:{i}"), str(obj["ref"])
                    )
                )
        return out


@dataclass(frozen=True)
class GitHubDeployments:
    repo: str
    environment: str = "production"
    token: SecretStr | None = field(default=None, repr=False)
    source: Source = "gh-actions"

    def fetch(self) -> list[DeployRecord]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token.get_secret_value()}"
        url: str | None = f"{GITHUB_API}/repos/{self.repo}/deployments"
        params: dict[str, Any] | None = {"environment": self.environment, "per_page": 100}
        out: list[DeployRecord] = []
        while url is not None:
            # The token rides in a header on every request: never follow a link off the API.
            if urlsplit(url).netloc != urlsplit(GITHUB_API).netloc:
                raise ValueError("a deployments page link left api.github.com; not followed")
            r = httpx.get(url, params=params, headers=headers, timeout=GITHUB_TIMEOUT_SECONDS)
            r.raise_for_status()
            out += [
                DeployRecord(_aware(str(d["created_at"]), self.repo), str(d["sha"]))
                for d in r.json()
            ]
            nxt = r.links.get("next")
            url, params = (nxt["url"], None) if nxt else (None, None)
        return out


def declared_feeds() -> dict[str, Any]:
    """deploys.yaml as a mapping of target -> entry."""
    path = get_settings().deploys_file
    declared = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(declared, dict):
        raise ValueError(f"{path.name}: a mapping of target -> deploy feed")
    return declared


def resolve_feed_path(raw: str, deploys_file: PurePath) -> PurePath:
    """An absolute path as given (a leading '/' counts, whatever the host OS); anything else
    relative to the directory deploys.yaml lives in."""
    path = type(deploys_file)(raw)
    if path.is_absolute() or raw.startswith("/"):
        return path
    return deploys_file.parent / path


def feed_for(target: str) -> DeployFeed:
    entry = declared_feeds().get(target)
    if not isinstance(entry, dict):
        raise LookupError(
            f"{target}: no deploy feed declared in {get_settings().deploys_file.name}"
        )
    if entry.get("source") == "self-host":
        return SelfHostLog(Path(resolve_feed_path(str(entry["path"]), get_settings().deploys_file)))
    if entry.get("source") == "gh-actions":
        return GitHubDeployments(
            str(entry["repo"]),
            str(entry.get("environment", "production")),
            get_settings().github_token,
        )
    raise LookupError(f"{target}: unknown deploy source {entry.get('source')!r}")


def ingest_deploys(
    target: str, *, feed: DeployFeed | None = None, now: dt.datetime | None = None
) -> int:
    """The number of deploys newly stored. Re-ingesting a feed stores nothing twice."""
    feed = feed or feed_for(target)
    now = now or dt.datetime.now(dt.UTC)
    if isinstance(feed, SelfHostLog) and feed.missing:
        notify_deploy_feed_missing(target, now=now)
        return 0
    records = {(r.deployed_at, r.ref) for r in feed.fetch()}
    if not records:
        return 0
    stmt = (
        insert(Deploy)
        .values(
            [
                {
                    "target": target,
                    "deployed_at": at,
                    "ref": ref,
                    "source": feed.source,
                    "ingested_at": now,
                }
                for at, ref in sorted(records)
            ]
        )
        .on_conflict_do_nothing(index_elements=["target", "deployed_at", "ref"])
        .returning(Deploy.ref)
    )
    with Session() as s:
        new = len(s.execute(stmt).all())
        s.commit()
    return new


def preceding_deploy(heal: HealEvent) -> Deploy | None:
    """The latest deploy of the heal's target after the previous run started and before the
    heal broke. None means this heal followed no deploy -- the control arm says it should not
    exist."""
    with Session() as s:
        previous = s.scalar(
            select(func.max(Run.first_started_at)).where(
                Run.target == heal.target,
                Run.run_key != heal.run_key,
                Run.first_started_at < heal.broke_at,
            )
        )
        q = select(Deploy).where(Deploy.target == heal.target, Deploy.deployed_at <= heal.broke_at)
        if previous is not None:
            q = q.where(Deploy.deployed_at > previous)
        return s.scalars(q.order_by(Deploy.deployed_at.desc()).limit(1)).first()
