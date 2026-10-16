"""One Prefect deployment per (target, spec), served by the worker container (design 9.2).

`python -m selector_drift.orchestrate.deployments` registers them with the Prefect server and
serves them; it blocks. Task 26C adds its deployment to the serve() call; Task 29A added the
fifteen-minute alert watch and Task 33 the daily evidence retention.
"""

from __future__ import annotations

import logging
from typing import cast

from prefect import serve
from prefect.deployments.runner import RunnerDeployment
from prefect.schedules import Cron

from selector_drift.browser.limits import MEM_LIMIT, PIDS_LIMIT
from selector_drift.config import get_settings
from selector_drift.heallog.retention import evidence_retention
from selector_drift.orchestrate.flows import extraction_flow
from selector_drift.orchestrate.watch import alert_watch
from selector_drift.quality.alerts import WEBHOOK_ENV, webhook_url
from selector_drift.spec.loader import load_spec

# Enforced by the worker container (docker-compose.yml, Task 4) under serve(); declared here so
# a move to a Docker work pool keeps them. test_deployments pins that the two agree. The values
# are named once, in browser/limits.py (Task 4).
JOB_LIMITS: dict[str, str | int] = {"mem_limit": MEM_LIMIT, "pids_limit": PIDS_LIMIT}
SCHEDULE_TIMEZONE = "UTC"
WATCH_CRON = "*/15 * * * *"
WATCH_JOB_LIMITS: dict[str, str | int] = {"mem_limit": "256m"}  # no browser in this flow
RETENTION_CRON = "30 3 * * *"  # daily, 03:30 UTC -- clear of the 04:12 extraction slot
# A declaration, not a limit: under serve() (a process runner in the worker container) Prefect
# applies no job_variables, and this flow is bounded by the worker service's own limits. Kept so
# a move to a Docker work pool starts from a stated number.
RETENTION_JOB_LIMITS: dict[str, str | int] = {"mem_limit": "512m"}

_log = logging.getLogger(__name__)


def extraction_deployments() -> list[RunnerDeployment]:
    out: list[RunnerDeployment] = []
    for path in sorted(get_settings().spec_dir.glob("*.yaml")):
        spec = load_spec(path)
        # `to_deployment` is `@sync_compatible`: mypy sees its declared return type as a union
        # with a coroutine even though this synchronous call always returns a RunnerDeployment.
        out.append(
            cast(
                RunnerDeployment,
                extraction_flow.to_deployment(
                    name=f"extract-{path.stem}",
                    schedules=[Cron(spec.schedule, timezone=SCHEDULE_TIMEZONE)],
                    parameters={"spec_path": path.name},
                    job_variables=dict(JOB_LIMITS),
                    tags=["extraction", path.stem],
                    description=f"{path.stem} v{spec.version}: {spec.row_scope}",
                ),
            )
        )
    return out


def watch_deployment() -> RunnerDeployment:
    """Missed heartbeats, false accepts and deploy ingest: the checks with no run to hang off."""
    return cast(
        RunnerDeployment,
        alert_watch.to_deployment(
            name="alert-watch",
            schedules=[Cron(WATCH_CRON, timezone=SCHEDULE_TIMEZONE)],
            job_variables=dict(WATCH_JOB_LIMITS),
            tags=["alerting"],
            description="every 15 minutes: missed heartbeats, false accepts, deploy history",
        ),
    )


def retention_deployment() -> RunnerDeployment:
    """Closes M30: client heal bundles, and the replay files held beside a decided quarantine,
    expire after the scope record's evidence_retention_days (heallog/retention.py); the
    heal_events rows and every count remain."""
    return cast(
        RunnerDeployment,
        evidence_retention.to_deployment(
            name="evidence-retention",
            schedules=[Cron(RETENTION_CRON, timezone=SCHEDULE_TIMEZONE)],
            job_variables=dict(RETENTION_JOB_LIMITS),
            tags=["retention"],
            description="daily: delete client heal bundles and replay files past their retention",
        ),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if webhook_url() is None:
        _log.error("%s is unset: refusals and failures will alert nobody", WEBHOOK_ENV)
    deployments = extraction_deployments()
    if not deployments:
        raise SystemExit(f"no task specs in {get_settings().spec_dir}")
    serve(*deployments, watch_deployment(), retention_deployment())


if __name__ == "__main__":
    main()
