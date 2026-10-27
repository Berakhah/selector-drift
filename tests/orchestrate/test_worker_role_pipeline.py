"""The grant list is exact: the real product, run with nothing but `sd_worker`'s privileges.

tests/db/test_worker_role.py pins what the role may and may not do. This is the other half --
that it may do enough. A cold-start run held in quarantine, a human promotion that lands the
rows and writes the cache, and the next day's cache-hit run exercise the worker's writes to runs,
attempts, heartbeats, resolutions, heal events, the quarantine, the selector cache, findings and
audit_log. A missing grant fails here, as `permission denied`, instead of at 04:12 on the VM.

The app's sessions are rebound to a connection that has done `SET ROLE sd_worker`; the test's own
fixtures (cleanup, assertions) stay on the administrator connection.

Callers: none (pytest collects tests/orchestrate/test_*.py). Instruction: "Postgres role and
append-onl".
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session as OrmSession

from selector_drift.db.session import Session, engine
from tests.db.test_worker_role import ROLE, _engine_as
from tests.orchestrate.conftest import Bench
from tests.orchestrate.test_pipeline_end_to_end import run, state_of
from tests.orchestrate.test_promotion_end_to_end import W0, W1, promote


@pytest.mark.integration
def test_a_run_a_promotion_and_a_cache_hit_run_need_only_the_workers_grants(
    prefect_api: None, bench: Bench, session: OrmSession
) -> None:
    acting = _engine_as(ROLE)
    Session.configure(bind=acting)  # every Session() the product opens now runs as sd_worker
    try:
        first = run(W0)
        assert state_of(first) == "quarantined"  # a cold start is held
        promote(first)  # lands the rows, writes the cache, audits the decision
        assert state_of(run(W1)) == "completed"  # the next day is an ordinary cache hit
    finally:
        Session.configure(bind=engine)
        acting.dispose()
