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
  no Playwright and no browsers. Non-root uid, as the worker image.
- `testbench/serve_scorer.py`: a thin module that calls Prefect `serve()` with the existing
  `bench-scorer` deployment (`score_pending_flow`, cron `42 4 * * *` UTC). It lives bench-side, so
  `selector_drift` still never imports `testbench`. Fails closed: if the entrypoint file is
  missing it exits non-zero rather than serving nothing.
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
  `SELECT` on `runs`, `heal_events`, `quarantine`; `INSERT` on `audit_log` (plus `USAGE` on its id
  sequence if the column is `serial` rather than an identity column; the migration test settles
  which); nothing else. The
  password is set at deploy time by `ALTER ROLE sd_scorer PASSWORD ...` from the VM's `.env`
  (Postgres init scripts run only on a fresh data volume, and no secret belongs in the repo).
- The evidence volume is mounted read-only. The bundle path is rebuilt from the heal id, as the
  Task 26C hardening already does.
- `SD_REDACTION_KEY` reaches the container through the environment from `.env`. It is not baked
  into the image and not in the repo. This is a second holder of the key; the user approved that
  on 2026-10-19.
- Hardening, as the other services: read-only root filesystem, `cap_drop: ALL`,
  `no-new-privileges`, non-root user, `mem_limit` and `pids_limit`, a `tmpfs` for `/tmp`,
  `PREFECT_HOME` under `/tmp`. Networked only to Postgres and the Prefect server.

## 5. Testing

- Role grants (integration): connect as `sd_scorer`; it can `SELECT` the three tables and `INSERT`
  into `audit_log`; it is refused `UPDATE` and `DELETE` on `audit_log`, any write to the other
  product tables, and a read of tables outside its grant.
- Compose (unit): pins the hardening flags, the read-only evidence mount and the absence of the
  browser stack, as `test_deployments` pins the worker's limits.
- Entrypoint (unit): `testbench.serve_scorer` registers exactly the declared deployment and exits
  non-zero when the entrypoint is missing.
- Import contracts: `lint-imports` stays at all contracts kept.
- Reviews at implementation: `ecc:database-reviewer` (migration), `ecc:security-reviewer`
  (compose, Dockerfile, secrets), `ecc:python-reviewer` (Python).

## 6. Out of scope

- Publishing the count of heals held pending behind unresolved quarantines.
- Checking that XPaths captured from a live browser match the answer key's.
- Re-running the 60-day campaign table in `docs/methodology.md`.
- A product-side database connect timeout (a separate decision).

## 7. Operational note

First deploy needs two manual steps on the VM: set `sd_scorer`'s password and add the scorer's
variables to `.env`. Until the migration is applied and the password set, the service fails to
start, loudly; it does not fall back to the product's database user.
