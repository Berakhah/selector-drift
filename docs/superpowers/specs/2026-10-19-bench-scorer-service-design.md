# Selector Drift — the bench-scorer service

Status: design approved 2026-10-19. Extends the test-bench portal spec
(`2026-09-26-selector-drift-testbench-portal.md`, §2 layout and §7 deployment).

## 1. Problem

Task 26C built `testbench/score.py`: it judges every bench heal against the answer key
(TRUE_ACCEPT, CORRECT_REFUSAL, FALSE_ACCEPT, FALSE_REFUSAL, or unscorable) and writes one
`audit_log` row per heal. `orchestrate/deployments.py` registers it by entrypoint string, but the
worker image ships `selector_drift/` only, so `bench_scorer_deployment()` returns `None` there and
the scorer never runs in production. Until it does, no false-accept count is produced and the Task
29A alert on false accepts cannot fire.

The product must not import or ship the bench (bench spec §2), and the portal image must stay
standalone (it must not import `selector_drift`). The scorer is the one bridge between them, so it
needs a home of its own.

## 2. Decision

A separate `bench-scorer` compose service, on the same VM and Docker network as the worker. The
product worker image stays free of `testbench/`; the portal image is unchanged.

Rejected: shipping `testbench/` in the worker image (the answer key would sit in the product
boundary, with the import contract as the only separation).

## 3. Components

- `docker/Dockerfile.bench-scorer`: `selector_drift` and `testbench`, `uv sync --frozen --no-dev`,
  no browsers (the Playwright Python package comes with the project's dependencies, but
  `playwright install` is never run). Non-root uid, as the worker image.
- `testbench/serve_scorer.py`: a thin module that calls Prefect `serve()` with the
  `bench-scorer` deployment built from `testbench.score.score_pending_flow` (cron `42 4 * * *`
  UTC, the same name and cron as `deployments.BENCH_SCORER`; a test pins that they agree). It
  imports only `prefect` and `testbench.score`: importing `selector_drift.orchestrate.deployments`
  would be an indirect path to `selector_drift.db`, which contract
  `only-the-scorer-reads-the-product` forbids for every bench module but `score`. It lives
  bench-side, so `selector_drift` still never imports `testbench`. A missing or broken
  `testbench.score` is an ImportError at start-up, so the service exits non-zero rather than
  serving nothing. `main()` first runs `testbench.score.preflight()`, which raises
  `ScorerNotReady` (non-zero exit, before `serve()`) unless the database role can read and insert
  into `audit_log` and the evidence root is a directory that does not expose `sessions`, `lake`
  or `corpus` (a Docker Engine older than 26 ignores `volume.subpath` and would mount the whole
  volume).
- `docker-compose.yml`: a `bench-scorer` service (section 4).
- `orchestrate/deployments.py`: the worker's missing-scorer message is softened from an error to an
  info line saying the scorer is served by its own service.
- `.importlinter`: expected to need no change; verified at implementation time.

## 4. Privileges and secrets

The scorer reads `runs`, `heal_events` and `quarantine`, reads heal bundles from disk, and writes one
`audit_log` row per heal. It takes a Postgres advisory lock (no grant needed). It also needs the
redaction key, because evidence redaction rewrites the seed tag and the scorer matches the
redacted form.

- Postgres role `sd_scorer`: created by an alembic migration with `LOGIN` and no password. Grants:
  `SELECT` on `runs`, `heal_events`, `quarantine`, `resolutions`, `audit_log` (the scorer joins
  `resolutions`, and reads `audit_log` to skip heals it already judged); `INSERT` on `audit_log`
  plus `USAGE` on `audit_log_id_seq` (the id is `Integer` autoincrement, i.e. `serial`); nothing
  else. The role is cluster-wide while every test worker migrates its own database, so the
  migration creates it idempotently, and its downgrade revokes the grants without dropping the
  role. The grant list is proven exact by running the real scorer as `sd_scorer`. The
  password is set at deploy time by `ALTER ROLE sd_scorer PASSWORD ...` from the VM's `.env`
  (Postgres init scripts run only on a fresh data volume, and no secret belongs in the repo).
- Only the `evidence` subpath of the `sd_data` volume is mounted, read-only (compose volume
  `subpath`, needs Compose 2.26+). The rest of `sd_data` holds injected client sessions and the
  lake; a second container gets none of it. The bundle path is rebuilt from the heal id, as the
  Task 26C hardening already does.
- `SD_REDACTION_KEY` reaches the container through the environment from `.env`, in the
  fail-closed `${VAR:?}` form. It is not baked into the image and not in the repo. Found while
  planning (2026-10-19): `docker-compose.yml` forwards the key to no service today, so the worker
  cannot redact evidence in compose either. The same variable is now forwarded to both services;
  they must hold the same key or the scorer cannot match the redacted seed tags. The scorer is a
  second holder, which the user approved on 2026-10-19.
- Hardening, as the other services: read-only root filesystem, `cap_drop: ALL`,
  `no-new-privileges`, non-root user, `mem_limit` and `pids_limit`, a `tmpfs` for `/tmp`,
  `PREFECT_HOME` under `/tmp`. It `depends_on` Postgres and the Prefect server and talks to
  nothing else, but compose puts every service on one default network and defines no `networks:`
  for any of them, so that is a dependency, not a segmentation. Segmenting the stack is a separate
  decision (it would cover the worker, the portal and Caddy too).

## 5. Testing

- Role grants (integration): a dedicated engine runs every connection as `sd_scorer` (`SET ROLE`,
  not a login: `sd_scorer` is cluster-wide and parallel test workers would race on its password).
  A privilege matrix over every public table pins `SELECT` on exactly the five tables, `INSERT` on
  `audit_log` only, and no `UPDATE`, `DELETE` or `TRUNCATE` anywhere; real statements are refused
  with SQLSTATE 42501; and the whole Task 26C scorer runs to completion as `sd_scorer`.
- Compose (unit): pins the hardening flags, the evidence `subpath` mount (read-only, nothing wider
  than `evidence`), the scorer's database user, no published ports, and the fail-closed form of
  `SD_REDACTION_KEY` and `SD_SCORER_PASSWORD`; and that worker and scorer read the same key
  variable.
- Entrypoint (unit): `testbench.serve_scorer` builds exactly one deployment, whose name, cron and
  entrypoint agree with `deployments.BENCH_SCORER`.
- Import contracts: `lint-imports` stays at all contracts kept.
- Reviews at implementation: `ecc:database-reviewer` (migration), `ecc:security-reviewer`
  (compose, Dockerfile, secrets), `ecc:python-reviewer` (Python).

## 6. Out of scope

- Publishing the count of heals held pending behind unresolved quarantines.
- Checking that XPaths captured from a live browser match the answer key's.
- Re-running the 60-day campaign table in `docs/methodology.md`.
- A product-side database connect timeout (a separate decision).

## 6a. Known blocker: no container image of this product builds from the repo today

`pyproject.toml` pins `bok-core` as an absolute host-path editable dependency (the documented
TEMPORARY workaround, P-1, `pyproject.toml:7-21`; `uv.lock` records the same path). `uv sync`
therefore cannot run inside any image, and the product's own `./Dockerfile` (the worker) hits it
exactly as `docker/Dockerfile.bench-scorer` does. This design does not change that. The scorer
image was verified in a scratch build outside the repo with `bok-core` vendored and `--frozen`
dropped (all 121 resolved packages matched `uv.lock`): imports resolve, uid is 10001, there are no
browser binaries, the deployment entrypoint inside the image is
`testbench/score.py:score_pending_flow`, and it loads under `--read-only --cap-drop ALL
--security-opt no-new-privileges`. Resolving P-1 (a PyPI release, a merged branch, or a vendored
wheel) unblocks both images.

## 7. Operational note

First deploy needs these manual steps on the VM, in order: add `SD_SCORER_PASSWORD` and
`SD_REDACTION_KEY` to `.env`; apply the migration (`alembic upgrade head`); create the evidence
directory once, because a `subpath` mount fails if it does not exist
(`docker compose run --rm --no-deps worker mkdir -p /data/evidence`); set the role's password
(the SQL is in `.env.example`). Until then the service fails to start, loudly:
`testbench.serve_scorer` runs `preflight()` before `serve()` and exits non-zero if the `sd_scorer`
role cannot read and insert into `audit_log`, or if the evidence mount is missing or exposes
sessions/lake/corpus; with `restart: unless-stopped` that is a visible restart loop. It does not
fall back to the product's database user.
