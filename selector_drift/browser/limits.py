"""Container limits for the browser worker (B8). Enforced by docker-compose's `worker` service;
declared on the Prefect deployment (Task 6) so a move to a Docker work pool keeps them."""

from __future__ import annotations

from typing import Final

MEM_LIMIT: Final = "2g"
PIDS_LIMIT: Final = 512
SHM_SIZE: Final = "1g"  # Chromium's default /dev/shm in Docker is 64 MB
FLOW_TIMEOUT_SECONDS: Final = 600  # B8: ten minutes, then Prefect cancels the run
