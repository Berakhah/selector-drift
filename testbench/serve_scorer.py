"""Serves the bench scorer from its own container (the `bench-scorer` compose service).

The product worker image ships selector_drift/ only, so it cannot serve the scorer
(orchestrate/deployments.py logs that and carries on). This module is the bench-side entrypoint:
it builds the same `bench-scorer` deployment from the flow itself and serves it. It imports only
prefect and testbench.score -- selector_drift.orchestrate.deployments would be an indirect path to
selector_drift.db, which contract only-the-scorer-reads-the-product allows for testbench.score
alone. The name, cron and timezone repeat deployments.BENCH_SCORER; test_serve_scorer pins that
the two agree.

A missing or broken testbench.score is an ImportError at start-up: the service exits non-zero
rather than serving nothing.
"""

from __future__ import annotations

import logging
from typing import cast

from prefect import serve
from prefect.deployments.runner import RunnerDeployment
from prefect.schedules import Cron

from testbench.score import score_pending_flow

NAME = "bench-scorer"
CRON = "42 4 * * *"  # 30 minutes after the bench's 04:12 run
TIMEZONE = "UTC"


def scorer_deployment() -> RunnerDeployment:
    # `to_deployment` is `@sync_compatible`: mypy sees a union with a coroutine although this
    # synchronous call always returns a RunnerDeployment (same cast as orchestrate/deployments.py).
    return cast(
        RunnerDeployment,
        score_pending_flow.to_deployment(
            name=NAME,
            schedules=[Cron(CRON, timezone=TIMEZONE)],
            tags=["bench"],
            description="daily, after the bench run: judge every bench heal against the answer key",
        ),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    serve(scorer_deployment())


if __name__ == "__main__":
    main()
