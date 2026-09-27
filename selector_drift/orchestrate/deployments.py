"""One Prefect deployment per (target, spec), served by the worker container (design 9.2).

`python -m selector_drift.orchestrate.deployments` registers them with the Prefect server and
serves them; it blocks. Tasks 26C and 33 add their deployments to the serve() call.
"""

from __future__ import annotations

import logging
from typing import cast

from prefect import serve
from prefect.deployments.runner import RunnerDeployment
from prefect.schedules import Cron

from selector_drift.browser.limits import MEM_LIMIT, PIDS_LIMIT
from selector_drift.config import get_settings
from selector_drift.orchestrate.flows import extraction_flow
from selector_drift.quality.alerts import WEBHOOK_ENV, webhook_url
from selector_drift.spec.loader import load_spec

# Enforced by the worker container (docker-compose.yml, Task 4) under serve(); declared here so
# a move to a Docker work pool keeps them. test_deployments pins that the two agree. The values
# are named once, in browser/limits.py (Task 4).
JOB_LIMITS: dict[str, str | int] = {"mem_limit": MEM_LIMIT, "pids_limit": PIDS_LIMIT}
SCHEDULE_TIMEZONE = "UTC"

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


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if webhook_url() is None:
        _log.error("%s is unset: refusals and failures will alert nobody", WEBHOOK_ENV)
    deployments = extraction_deployments()
    if not deployments:
        raise SystemExit(f"no task specs in {get_settings().spec_dir}")
    serve(*deployments)


if __name__ == "__main__":
    main()
