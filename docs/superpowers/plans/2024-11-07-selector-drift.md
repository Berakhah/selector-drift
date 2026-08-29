# Selector Drift Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A scheduled extraction pipeline that re-resolves page elements when a UI changes, proves each heal correct through an unskippable validation gate, and refuses to write when it cannot.

**Architecture:** A type-gated spine. Each stage returns a type the next stage structurally requires, so the two safety gates cannot be bypassed by forgetting to call them. `scope/` is the only module that can mint an `AuthorisedTarget`; `validate/` is the only module that can mint a `ValidatedExtraction`; `land.write()` accepts nothing else. A five-rung resolution cascade heals broken selectors, and a six-check gate judges every heal — including a cross-family re-resolution that catches the adjacent-column failure with no history needed.

**Tech Stack:** Python 3.12+, `uv`, `ruff`, `mypy --strict`, `import-linter`, Pydantic v2, Playwright (containerised, pinned), Prefect 3, Postgres + Alembic, Parquet + DuckDB, FastAPI, Next.js 15 + `bok-ui`, `bok-core` (PyPI), pytest + hypothesis, axe-core.

**Spec:** [`docs/superpowers/specs/2024-11-07-selector-drift-design.md`](../specs/2024-11-07-selector-drift-design.md)

## Global Constraints

- Python 3.12+, `uv` for dependencies, `ruff` for lint+format, `mypy --strict` on every module. TypeScript strict mode, no `any` without a justifying comment.
- Licence **Apache-2.0**. Repo **private until first release (week 20)**, per the programme decision — flipping a repo public preserves its full git history, so the ninety-day log's start date stays verifiable, and nightly runs fit inside the private-repo Actions allowance.
- **$0 recurring cost.** No dependency with a paid floor.
- **No secrets in the repo, ever, including fixtures.** `gitleaks` pre-commit + CI.
- **Zero model calls in v1.** No `bok_core.gateway` import anywhere. Rung 5 escalates; it does not call a model.
- **No `[project.scripts]`, no Typer/Click.** Dev entrypoint is `python -m selector_drift`. Buyer entrypoint is the Prefect scheduler.
- **No TOTP seed storage.** No field named or typed for a TOTP seed exists anywhere in the schema. Enforced by a test (Task 31).
- **No write-back into a client portal.** `land/` writes to our lake only; the change-set export (Task 30) produces a file the client applies.
- Scope bases in v1 are exactly `self-owned` and `client-authorised`. `public-permissive` is refused.
- Everything runs from `docker compose up` with only `.env.example` copied.
- Coverage floor 75%. axe zero serious/critical.
- Every subprocess containerised: non-root, read-only rootfs, tmpfs scratch, seccomp default, memory and pid limits, hard timeout.

**Standing verification commands** (referenced as *the full check* below):

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict selector_drift
uv run lint-imports
uv run pytest -q --cov=selector_drift --cov-fail-under=75
```

---

# Phase A — Day one. The log starts. (Week 13, day 1)

**Tasks 1–6 are one block and must all land on day one of week 13.** The ninety-day log takes calendar time that cannot be compressed. Do not begin Task 7 until the Prefect cron has fired at least once and written a run record. Day one logs *run succeeded / run failed* and nothing more; heal events appear around week 15 as the cascade lands, and the start date stays honest because the run record was always there.

---

### Task 1: Scaffold the repo and lock the import contracts

**Files:**
- Create: whole repo via `copier copy gh:USER/backoffice-kit .`
- Create: `.importlinter`
- Create: `pyproject.toml` (from template; add deps)
- Test: `tests/test_import_contracts.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a repo where `uv run lint-imports` is a real CI gate. Every later task depends on this gate existing before the code it constrains does.

- [ ] **Step 1: Scaffold from the copier template**

```bash
cd selector-drift
uv tool run copier copy gh:USER/backoffice-kit . \
  --data project_name=selector-drift \
  --data accent_oklch="0.55 0.14 268" \
  --data licence=Apache-2.0
```

- [ ] **Step 2: Add this project's dependencies**

```bash
uv add pydantic playwright prefect duckdb pyarrow fastapi sqlalchemy alembic psycopg pyyaml scipy bok-core
uv add --dev pytest pytest-cov hypothesis import-linter mypy ruff
uv run playwright install --with-deps chromium
```

Pin the browser build in `pyproject.toml` under `[tool.selector-drift]` as `playwright_chromium_revision = "<revision printed by the install>"`. B8: an unpinned browser turns a UI-change test into a browser-change test.

- [ ] **Step 3: Write the import contracts**

Create `.importlinter`:

```ini
[importlinter]
root_packages = selector_drift, testbench

[importlinter:contract:resolver-cannot-write]
name = A resolver cannot write
type = forbidden
source_modules = selector_drift.resolve
forbidden_modules = selector_drift.land

[importlinter:contract:judged-cannot-call-judge]
name = The judged cannot call the judge
type = forbidden
source_modules = selector_drift.resolve
forbidden_modules = selector_drift.validate

[importlinter:contract:heallog-cannot-refetch]
name = Replay uses stored evidence, never a live page
type = forbidden
source_modules = selector_drift.heallog
forbidden_modules = selector_drift.browser

[importlinter:contract:answer-key-isolation]
name = The resolver cannot see the answer key
type = forbidden
source_modules = selector_drift
forbidden_modules = testbench.answers

[importlinter:contract:no-model-calls]
name = v1 makes zero model calls
type = forbidden
source_modules = selector_drift
forbidden_modules = bok_core.gateway
```

- [ ] **Step 4: Write the failing test**

```python
# tests/test_import_contracts.py
import subprocess

def test_import_contracts_pass():
    r = subprocess.run(["uv", "run", "lint-imports"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr

def test_answer_key_contract_is_declared():
    assert "answer-key-isolation" in open(".importlinter").read()
```

- [ ] **Step 5: Run it and watch it fail**

Run: `uv run pytest tests/test_import_contracts.py -v`
Expected: FAIL — the packages named in `.importlinter` do not exist yet.

- [ ] **Step 6: Create the package skeleton**

```bash
mkdir -p selector_drift/{spec,scope,credentials,browser,resolve/rungs,validate/assertions,extract,quality,land,orchestrate,heallog,api}
mkdir -p testbench/{portal,mutations,answers}
for d in $(find selector_drift testbench -type d); do touch "$d/__init__.py"; done
```

- [ ] **Step 7: Run the tests and make sure they pass**

Run: `uv run pytest tests/test_import_contracts.py -v && uv run lint-imports`
Expected: PASS. `lint-imports` prints `Contracts: 5 kept, 0 broken.`

- [ ] **Step 8: Commit**

```bash
git init && git add -A
git commit -m "chore: scaffold selector-drift with import contracts"
gh repo create selector-drift --private --source=. --push
# Flip to public at week 20 (Task 41). History — and therefore the log start date — is preserved.
```

---

### Task 2: The scope gate — build the refusal before the crawler

**Files:**
- Create: `selector_drift/scope/record.py`, `selector_drift/scope/gate.py`, `selector_drift/scope/errors.py`
- Test: `tests/scope/test_gate.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ScopeRecord` (Pydantic v2) with fields `target: str`, `basis: Basis`, `authorising_party: str`, `authorised_on: date`, `expires_on: date`, `evidence_uri: str | None`, `rate_limit: RateLimit`, `user_agent_contact: str`, `first_heal_policy: FirstHealPolicy`, `legal_review: str`, `sensitivity: Sensitivity`
  - `Basis` — `StrEnum` with exactly `SELF_OWNED = "self-owned"` and `CLIENT_AUTHORISED = "client-authorised"`
  - `FirstHealPolicy` — `StrEnum` with `REQUIRED = "required"`, `AUTO_PROMOTE = "auto_promote"`
  - `RateLimit(requests: int, per_seconds: int)`
  - `AuthorisedTarget` — frozen dataclass; **constructor requires a `_ScopeToken`**, a module-private class instantiated only inside `gate.py`
  - `authorise(record: ScopeRecord, *, today: date) -> AuthorisedTarget` — raises `ScopeRefusal`
  - `ScopeRefusal(Exception)` with `.reason: str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/scope/test_gate.py
import datetime as dt
import pytest
from selector_drift.scope.record import ScopeRecord, Basis, AuthorisedTarget
from selector_drift.scope.gate import authorise
from selector_drift.scope.errors import ScopeRefusal

TODAY = dt.date(2026, 9, 7)

def rec(**kw):
    base = dict(
        target="testbench", basis=Basis.SELF_OWNED, authorising_party="me",
        authorised_on=dt.date(2026, 9, 1), expires_on=dt.date(2027, 9, 1),
        evidence_uri=None, rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required", legal_review="self_drafted",
        sensitivity="SYNTHETIC",
    )
    return ScopeRecord(**(base | kw))

def test_valid_self_owned_record_is_authorised():
    assert authorise(rec(), today=TODAY).target == "testbench"

def test_expired_authorisation_is_refused():
    with pytest.raises(ScopeRefusal, match="expired"):
        authorise(rec(expires_on=dt.date(2026, 9, 6)), today=TODAY)

def test_public_permissive_basis_does_not_exist():
    with pytest.raises(ValueError):
        rec(basis="public-permissive")

def test_client_authorised_without_evidence_is_refused():
    with pytest.raises(ScopeRefusal, match="evidence_uri"):
        authorise(rec(basis=Basis.CLIENT_AUTHORISED, evidence_uri=None), today=TODAY)

def test_authorised_target_cannot_be_built_outside_the_gate():
    with pytest.raises(TypeError):
        AuthorisedTarget(target="anything", basis=Basis.SELF_OWNED)  # type: ignore[call-arg]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/scope -v`
Expected: FAIL — `ModuleNotFoundError: selector_drift.scope.record`.

- [ ] **Step 3: Implement the record and the token-gated mint site**

`record.py` defines the enums, `ScopeRecord`, and:

```python
class _ScopeToken:
    """Private. Instantiated only inside gate.py. This is the mint control."""

@dataclass(frozen=True)
class AuthorisedTarget:
    _token: _ScopeToken
    target: str
    basis: Basis
    rate_limit: RateLimit
    user_agent_contact: str
    first_heal_policy: FirstHealPolicy
    sensitivity: Sensitivity
    expires_on: date
```

`gate.py` implements `authorise()`: refuse if `expires_on < today`; refuse if `basis is CLIENT_AUTHORISED and evidence_uri is None`; otherwise return `AuthorisedTarget(_ScopeToken(), ...)`.

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `uv run pytest tests/scope -v && uv run mypy --strict selector_drift/scope`
Expected: PASS, 5 tests. mypy clean.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/scope tests/scope
git commit -m "feat(scope): authorisation gate, fails closed, two bases only"
```

---

### Task 3: The full run and heal-event schema — day one, most columns null

**Files:**
- Create: `selector_drift/db/models.py`, `selector_drift/db/session.py`, `alembic/versions/0001_initial.py`
- Test: `tests/db/test_schema.py`

**Interfaces:**
- Consumes: `Sensitivity`, `Basis` from Task 2.
- Produces: tables `runs`, `heal_events`, `selector_cache`, `quarantine`, `audit_log`, `scope_records`. Later tasks write to these; **none of them may add a column afterwards**, because you cannot compare day 1 to day 90 across a schema migration. This is the one place the plan deliberately builds ahead of need.

Column list, fixed now:

| Table | Columns |
|---|---|
| `runs` | `run_key` (PK, `f"{target}:{spec_version}:{window}"`), `target`, `spec_version`, `window`, `started_at`, `finished_at`, `state` (`completed`/`healed`/`quarantined`/`failed`/`refused`), `rows_written`, `error` |
| `heal_events` | `id`, `run_key` FK, `target`, `field`, `broke_at`, `winning_rung`, `confirmer_rung`, `selector_before`, `selector_after`, `anchor_before` (JSONB), `anchor_after` (JSONB), `trace` (JSONB), `verdicts` (JSONB), `evidence_uri`, `replayed_ok` (nullable bool), `promoted_by`, `promoted_at` |
| `selector_cache` | `target`, `field` (composite PK), `selector`, `anchor` (JSONB), `set_by_rung`, `set_at` |
| `quarantine` | `run_key` FK, `reason`, `verdicts` (JSONB), `data_uri`, `resolved_as` (`promoted`/`discarded`/null), `actor`, `resolved_at` |
| `audit_log` | `id`, `at`, `actor`, `action`, `subject`, `detail` (JSONB) |
| `scope_records` | mirror of `ScopeRecord` fields plus `loaded_at` |

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_schema.py
from sqlalchemy import inspect
from selector_drift.db.session import engine

EXPECTED = {"runs", "heal_events", "selector_cache", "quarantine", "audit_log", "scope_records"}

def test_all_tables_exist():
    assert EXPECTED <= set(inspect(engine).get_table_names())

def test_heal_events_carries_anchor_columns_from_day_one():
    cols = {c["name"] for c in inspect(engine).get_columns("heal_events")}
    assert {"anchor_before", "anchor_after", "confirmer_rung", "replayed_ok"} <= cols
```

- [ ] **Step 2: Run it and watch it fail**

Run: `docker compose up -d postgres && uv run pytest tests/db -v`
Expected: FAIL — no such table `runs`.

- [ ] **Step 3: Write the models and the migration**

Declare the tables above in `models.py`, then:

```bash
uv run alembic revision --autogenerate -m "initial schema"
uv run alembic upgrade head
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `uv run pytest tests/db -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/db alembic tests/db
git commit -m "feat(db): full run and heal-event schema, fixed for the 90-day log"
```

---

### Task 4: The browser driver that cannot be handed a bare URL

**Files:**
- Create: `selector_drift/browser/driver.py`, `selector_drift/browser/registry.py`, `selector_drift/browser/limits.py`
- Modify: `docker-compose.yml` (add the `worker` service)
- Test: `tests/browser/test_driver.py`

**Interfaces:**
- Consumes: `AuthorisedTarget` (Task 2).
- Produces:
  - `open(target: AuthorisedTarget, credentials: Credentials | None = None) -> Session` — async context manager
  - `Session.page: PageHandle`, `PageHandle.url: str`, `PageHandle.content() -> str`, `PageHandle.screenshot() -> bytes`
  - `registry.url_for(target: str) -> str` — the only place a URL string lives
  - **No function in this module accepts a `str` URL.** Enforced by test.

- [ ] **Step 1: Write the failing tests**

```python
# tests/browser/test_driver.py
import inspect, pytest
from selector_drift import browser

def test_no_public_function_accepts_a_bare_url():
    for name, fn in inspect.getmembers(browser.driver, inspect.isfunction):
        if name.startswith("_"):
            continue
        hints = inspect.get_annotations(fn, eval_str=True)
        assert hints.get("url") is not str, f"{name} accepts a bare URL"

@pytest.mark.asyncio
async def test_open_loads_a_page(authorised_testbench):
    async with browser.driver.open(authorised_testbench) as session:
        assert "<html" in (await session.page.content()).lower()
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/browser -v`
Expected: FAIL — `AttributeError: module 'selector_drift.browser' has no attribute 'driver'`.

- [ ] **Step 3: Implement the driver**

`open()` reads the URL via `registry.url_for(target.target)`, applies `target.rate_limit` (the limit comes from the `AuthorisedTarget`, never from config), sets the user agent to include `target.user_agent_contact`, launches **one browser context per run**, and tears it down in `__aexit__`.

`limits.py` holds the container limits applied at the Prefect deployment level in Task 6.

- [ ] **Step 4: Add the worker service to compose**

```yaml
  worker:
    build: .
    read_only: true
    user: "10001:10001"
    security_opt: [ "seccomp=unconfined" ]
    tmpfs: [ "/tmp" ]
    mem_limit: 2g
    pids_limit: 512
    depends_on: [ postgres, prefect-server ]
```

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `uv run pytest tests/browser -v`
Expected: PASS, 2 tests.

- [ ] **Step 6: Commit**

```bash
git add selector_drift/browser tests/browser docker-compose.yml
git commit -m "feat(browser): AuthorisedTarget-only driver, one context per run"
```

---

### Task 5: Three scope records and a one-field task spec per target

**Files:**
- Create: `selector_drift/spec/schema.py`, `selector_drift/spec/loader.py`
- Create: `scopes/testbench.yaml`, `scopes/agent-perimeter.yaml`, `scopes/ledger-sense.yaml`
- Create: `specs/testbench.yaml`, `specs/agent-perimeter.yaml`, `specs/ledger-sense.yaml`
- Test: `tests/spec/test_loader.py`, `tests/scope/test_records_load.py`

**Interfaces:**
- Consumes: `ScopeRecord`, `authorise` (Task 2).
- Produces:
  - `TaskSpec` with `version: int`, `scope_ref: str`, `navigate: list[NavStep]`, `targets: list[FieldTarget]`, `schedule: str`
  - `FieldTarget(name: str, description: str, relation: Relation | None, assertions: list[AssertionSpec])` — `description` is the semantic target in plain language, never a DOM path
  - `Relation` — `StrEnum` `RIGHT_OF`, `BELOW`, `WITHIN`
  - `load_spec(path: Path) -> TaskSpec`, `load_scope(path: Path) -> ScopeRecord`

`scopes/testbench.yaml` — note `first_heal_policy: auto_promote`, permitted **only because the bench is synthetic with a known answer key** (spec §5.8):

```yaml
target: testbench
basis: self-owned
authorising_party: <your name>
authorised_on: 2026-09-07
expires_on: 2027-09-07
evidence_uri: null
rate_limit: { requests: 1, per_seconds: 30 }
user_agent_contact: https://github.com/USER/selector-drift
first_heal_policy: auto_promote
legal_review: self_drafted
sensitivity: SYNTHETIC
```

The other two are identical but `first_heal_policy: required`.

`specs/testbench.yaml`:

```yaml
version: 1
scope_ref: scopes/testbench.yaml
schedule: "12 4 * * *"
navigate:
  - { step: goto, path: /login }
  - { step: fill, field: username, value_from: credentials.username }
  - { step: fill, field: password, value_from: credentials.password }
  - { step: click, role: button, name: Sign in }
  - { step: goto, path: /results }
targets:
  - name: claim_number
    description: the claim number cell in the results table
    relation: right_of
    assertions:
      - { type: format, pattern: '^[A-Z]{2}\d{8}$' }
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/scope/test_records_load.py
import datetime as dt
from pathlib import Path
from selector_drift.spec.loader import load_scope
from selector_drift.scope.gate import authorise

def test_all_three_day_one_targets_authorise():
    names = {"testbench", "agent-perimeter", "ledger-sense"}
    got = {authorise(load_scope(p), today=dt.date.today()).target
           for p in Path("scopes").glob("*.yaml")}
    assert got == names

def test_only_the_bench_auto_promotes():
    auto = {load_scope(p).target for p in Path("scopes").glob("*.yaml")
            if load_scope(p).first_heal_policy == "auto_promote"}
    assert auto == {"testbench"}
```

```python
# tests/spec/test_loader.py
from pathlib import Path
from selector_drift.spec.loader import load_spec

def test_a_target_is_described_in_words_not_a_dom_path():
    t = load_spec(Path("specs/testbench.yaml")).targets[0]
    assert " " in t.description and "#" not in t.description and "." not in t.description

def test_every_target_declares_at_least_one_assertion():
    for p in Path("specs").glob("*.yaml"):
        assert all(t.assertions for t in load_spec(p).targets)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/spec tests/scope/test_records_load.py -v`
Expected: FAIL — `scopes/` and `specs/` are empty.

- [ ] **Step 3: Write the schema, the loader, and the six YAML files**

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `uv run pytest tests/spec tests/scope -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/spec scopes specs tests/spec tests/scope
git commit -m "feat(spec): three day-one scope records and task specs"
```

---

### Task 6: The cron fires and writes a run record — the log has started

**Files:**
- Create: `selector_drift/orchestrate/flows.py`, `selector_drift/orchestrate/deployments.py`, `selector_drift/orchestrate/rung_stats.py`
- Create: `selector_drift/__main__.py`
- Test: `tests/orchestrate/test_run_record.py`

**Interfaces:**
- Consumes: `authorise`, `browser.driver.open`, `load_spec`, the `runs` table.
- Produces:
  - `run_key(target: str, spec_version: int, window: str) -> str` — deterministic, `f"{target}:{spec_version}:{window}"`. **Every later task uses this exact function for idempotency.**
  - `extraction_flow(spec_path: str) -> str` — Prefect flow, returns the `run_key`
  - `rung_stats() -> dict[str, float]` — proportion of resolutions won by each rung, keys `rung1`…`rung5`. Empty until Phase C. Task 26 and Task 41 both read it; it is the source of a published claim (B11).

- [ ] **Step 1: Write the failing tests**

```python
# tests/orchestrate/test_run_record.py
from selector_drift.orchestrate.flows import extraction_flow, run_key
from selector_drift.db.session import Session
from selector_drift.db.models import Run

def test_flow_writes_a_run_record():
    key = extraction_flow("specs/testbench.yaml")
    with Session() as s:
        assert s.get(Run, key).state in {"completed", "failed"}

def test_run_key_is_deterministic():
    assert run_key("t", 1, "2026-09-07") == run_key("t", 1, "2026-09-07")

def test_a_refused_target_never_opens_a_browser(monkeypatch, expired_spec):
    opened = []
    monkeypatch.setattr("selector_drift.browser.driver.open", lambda *a, **k: opened.append(1))
    key = extraction_flow(expired_spec)
    with Session() as s:
        assert s.get(Run, key).state == "refused"
    assert opened == []
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/orchestrate -v`
Expected: FAIL — `ModuleNotFoundError: selector_drift.orchestrate.flows`.

- [ ] **Step 3: Implement the flow**

`extraction_flow` authorises the target, opens the page, records `started_at`, does **nothing else** — no cascade exists yet — then writes `state="completed"` or `state="failed"` with the error. `ScopeRefusal` writes `state="refused"` and never opens a browser.

- [ ] **Step 4: Register the deployments with container limits**

```bash
uv run python -m selector_drift.orchestrate.deployments
uv run prefect deployment ls
```

Each deployment sets `job_variables={"mem_limit": "2g", "pids_limit": 512}` and a 10-minute timeout (B8).

- [ ] **Step 5: Verify the cron actually fires**

```bash
docker compose up -d
uv run prefect flow-run ls --limit 5
psql -c "select run_key, state, started_at from runs order by started_at desc limit 5"
```

Expected: at least one row per target. **The ninety-day log has now started. Record today's date in `docs/methodology.md` as the per-target start date for all three targets.**

- [ ] **Step 6: Run the tests and make sure they pass**

Run: `uv run pytest tests/orchestrate -v`
Expected: PASS, 3 tests.

- [ ] **Step 7: Commit**

```bash
git add selector_drift/orchestrate selector_drift/__main__.py tests/orchestrate docs/methodology.md
git commit -m "feat(orchestrate): scheduled runs writing run records — 90-day log start"
```

---

# Phase B — The test bench (weeks 13–14)

A prerequisite for testing the cascade, not a week-19 afterthought. You cannot test a resolver against pages that do not change.

---

### Task 7: The portal

**Files:**
- Create: `testbench/portal/app/login/page.tsx`, `app/search/page.tsx`, `app/results/page.tsx`, `app/detail/[id]/page.tsx`, `app/export/route.ts`
- Create: `testbench/portal/lib/data.ts` — synthetic claim rows
- Test: `testbench/portal/e2e/portal.spec.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: a served portal whose results table has columns `Claim Number`, `Claimant`, `Amount`, `Status`, `Filed`. `Claim Number` matches `^[A-Z]{2}\d{8}$`. **`Amount` and a decoy `Prior Amount` are deliberately similar in shape** — Task 11 exploits this.

- [ ] **Step 1: Write the failing E2E test**

```typescript
// testbench/portal/e2e/portal.spec.ts
test('results table renders five columns of synthetic claims', async ({ page }) => {
  await page.goto('/login');
  await page.fill('#username', 'demo'); await page.fill('#password', 'demo');
  await page.click('button[type=submit]');
  await page.goto('/results');
  await expect(page.getByRole('columnheader')).toHaveCount(5);
  await expect(page.getByRole('cell').first()).toHaveText(/^[A-Z]{2}\d{8}$/);
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd testbench/portal && npx playwright test`
Expected: FAIL — no such route.

- [ ] **Step 3: Build the five pages with synthetic data**

- [ ] **Step 4: Run it and make sure it passes**

Run: `cd testbench/portal && npx playwright test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add testbench/portal
git commit -m "feat(testbench): realistic portal with synthetic claims"
```

---

### Task 8: The mutation engine and the answer key the resolver cannot see

**Files:**
- Create: `testbench/mutations/base.py`, `testbench/schedule.py`, `testbench/answers/key.py`
- Create: `testbench/portal/middleware.ts`
- Test: `tests/testbench/test_schedule.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Mutation` protocol — `name: str`, `apply(html: str) -> str`, pure
  - `mutations_for(day: date, target: str) -> list[Mutation]` — seeded, `seed = sha256(f"{day}:{target}")`
  - `testbench.answers.key.expected_selector(day: date, field: str) -> str` — **importable only by tests and the scorer**, never by `selector_drift/` (Task 1 contract)

- [ ] **Step 1: Write the failing tests**

```python
# tests/testbench/test_schedule.py
import datetime as dt
from testbench.schedule import mutations_for

def test_schedule_is_deterministic_for_a_day():
    a = [m.name for m in mutations_for(dt.date(2026, 9, 9), "testbench")]
    b = [m.name for m in mutations_for(dt.date(2026, 9, 9), "testbench")]
    assert a == b

def test_different_days_differ():
    a = [m.name for m in mutations_for(dt.date(2026, 9, 9), "testbench")]
    b = [m.name for m in mutations_for(dt.date(2026, 9, 10), "testbench")]
    assert a != b
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/testbench -v`
Expected: FAIL — `ModuleNotFoundError: testbench.schedule`.

- [ ] **Step 3: Implement the seeded schedule and the Next.js middleware**

The middleware calls `mutations_for(today, "testbench")` and pipes the rendered HTML through each mutation's `apply()` before returning it. No commits, no redeploys — the served markup changes on its own.

- [ ] **Step 4: Run the tests and confirm the contract holds**

Run: `uv run pytest tests/testbench -v && uv run lint-imports`
Expected: PASS. `answer-key-isolation` kept.

- [ ] **Step 5: Commit**

```bash
git add testbench/mutations testbench/schedule.py testbench/answers testbench/portal/middleware.ts tests/testbench
git commit -m "feat(testbench): seeded mutation engine with isolated answer key"
```

---

### Task 9: Structural mutation classes

**Files:**
- Create: `testbench/mutations/class_rename.py`, `dom_restructure.py`, `column_reorder.py`, `table_to_div.py`
- Test: `tests/testbench/test_structural_mutations.py`

**Interfaces:**
- Consumes: `Mutation` (Task 8).
- Produces: `ClassRename`, `DomRestructure`, `ColumnReorder`, `TableToDiv`. Task 26 asserts the cascade heals each.

- [ ] **Step 1: Write the failing tests**

```python
# tests/testbench/test_structural_mutations.py
import pytest
from testbench.mutations.column_reorder import ColumnReorder
from testbench.mutations.class_rename import ClassRename
from testbench.mutations.dom_restructure import DomRestructure
from testbench.mutations.table_to_div import TableToDiv
from tests.testbench.helpers import render_fixture, cells

@pytest.mark.parametrize("mutation", [ClassRename(), DomRestructure(), ColumnReorder(), TableToDiv()])
def test_mutation_changes_markup_but_preserves_values(mutation):
    before = render_fixture()
    after = mutation.apply(before)
    assert before != after
    assert sorted(cells(before)) == sorted(cells(after))

def test_column_reorder_actually_moves_positions():
    before = render_fixture()
    assert cells(before) != cells(ColumnReorder().apply(before))
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/testbench/test_structural_mutations.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the four transforms** with `lxml` over the served HTML.

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/testbench -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add testbench/mutations tests/testbench/test_structural_mutations.py
git commit -m "feat(testbench): structural mutation classes"
```

---

### Task 10: Semantic mutation classes

**Files:**
- Create: `testbench/mutations/label_reword.py`, `id_churn.py`, `framework_markup.py`, `pagination.py`
- Test: `tests/testbench/test_semantic_mutations.py`

**Interfaces:**
- Consumes: `Mutation` (Task 8).
- Produces: `LabelReword`, `IdChurn`, `FrameworkMarkup`, `Pagination`, bringing the suite to eight.

- [ ] **Step 1: Write the failing tests**

```python
# tests/testbench/test_semantic_mutations.py
import pytest
from testbench.mutations.label_reword import LabelReword
from testbench.mutations.id_churn import IdChurn
from testbench.mutations.framework_markup import FrameworkMarkup
from testbench.mutations.pagination import Pagination
from tests.testbench.helpers import render_fixture, cells, headers

@pytest.mark.parametrize("mutation", [LabelReword(), IdChurn(), FrameworkMarkup(), Pagination()])
def test_mutation_changes_markup_but_preserves_values(mutation):
    before = render_fixture()
    after = mutation.apply(before)
    assert before != after
    assert sorted(cells(before)) == sorted(cells(after))

def test_label_reword_changes_the_header_and_nothing_else():
    after = LabelReword().apply(render_fixture())
    assert "Claim No." in headers(after) and "Claim Number" not in headers(after)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/testbench/test_semantic_mutations.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the four transforms.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/testbench -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add testbench/mutations tests/testbench/test_semantic_mutations.py
git commit -m "feat(testbench): semantic mutation classes"
```

---

### Task 11: The adversarial mutation — the decoy column

**Files:**
- Create: `testbench/mutations/adversarial/decoy_column.py`
- Test: `tests/testbench/test_adversarial.py`

**Interfaces:**
- Consumes: `Mutation` (Task 8).
- Produces: `DecoyColumn` — reorders columns **and** inserts a `Prior Amount` column whose values are plausible amounts. A resolver keying on column index heals to the wrong column and every individual value still looks valid. **This is the mutation the whole validation gate exists to refuse.**

- [ ] **Step 1: Write the failing tests**

```python
# tests/testbench/test_adversarial.py
from testbench.mutations.adversarial.decoy_column import DecoyColumn
from tests.testbench.helpers import render_fixture, column, is_plausible_amount

def test_decoy_is_indistinguishable_by_value_alone():
    after = DecoyColumn().apply(render_fixture())
    real, decoy = column(after, "Amount"), column(after, "Prior Amount")
    assert all(is_plausible_amount(v) for v in real + decoy)
    assert real != decoy

def test_decoy_sits_adjacent_to_the_real_column():
    after = DecoyColumn().apply(render_fixture())
    hs = headers(after)
    assert abs(hs.index("Amount") - hs.index("Prior Amount")) == 1
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/testbench/test_adversarial.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `DecoyColumn`.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/testbench -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add testbench/mutations/adversarial tests/testbench/test_adversarial.py
git commit -m "feat(testbench): adversarial decoy-column mutation"
```

---

### Task 12: Deploy the bench and point the log at it

**Files:**
- Create: `.github/workflows/deploy-testbench.yml`
- Create: `testbench/README.md` (mutation engine documented — DoD item 5)
- Modify: `selector_drift/browser/registry.py`
- Modify: `docs/methodology.md`

**Interfaces:**
- Consumes: `registry.url_for` (Task 4).
- Produces: a stable deployed bench URL that mutates by date without a redeploy.

- [ ] **Step 1: Deploy to Vercel free tier** with root directory `testbench/portal`.

- [ ] **Step 2: Verify the deployed markup mutates by date**

```bash
curl -s "$BENCH_URL/results" | sha256sum
TESTBENCH_DATE_OVERRIDE=2026-09-10 curl -s "$BENCH_URL/results" | sha256sum
```

Expected: two different hashes.

- [ ] **Step 3: Point the registry at the deployed URL and confirm the cron still writes run records**

Run: `psql -c "select state, count(*) from runs group by state"`
Expected: `completed` rows continuing to accumulate for `testbench`.

- [ ] **Step 4: Write `testbench/README.md`** documenting every mutation class, the seeding scheme, and how to reproduce a given day's DOM.

- [ ] **Step 5: Record the bench's start date and mutation schedule in `docs/methodology.md`.**

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/deploy-testbench.yml testbench/README.md selector_drift/browser/registry.py docs/methodology.md
git commit -m "chore(testbench): deploy and wire into the 90-day log"
```

---

# Phase C — The cascade (weeks 14–16)

---

### Task 13: The `Rung` protocol, the trace, and the runner

**Files:**
- Create: `selector_drift/resolve/rungs/base.py`, `selector_drift/resolve/trace.py`, `selector_drift/resolve/cascade.py`
- Test: `tests/resolve/test_cascade.py`

**Interfaces:**
- Consumes: `PageHandle` (Task 4), `FieldTarget` (Task 5).
- Produces:
  - `Family` — `StrEnum` with `TEXT_SEMANTIC`, `STRUCTURAL`, `NONE`
  - `Rung` protocol — `rung_id: int`, `family: Family`, `resolve(page: PageHandle, target: FieldTarget) -> ResolvedElement | None`
  - `AnchorEvidence(accessible_name: str | None, label_text: str | None, header_text: str | None)`
  - `ResolvedElement(selector: str, anchor: AnchorEvidence, rung_id: int)`
  - `RungAttempt(rung_id: int, ok: bool, reason: str, ms: int)`
  - `CascadeTrace(attempts: list[RungAttempt], winner: int | None)` with property `.healed: bool` — **`True` when `winner != 1`**. Task 20's asymmetry reads exactly this property.
  - `resolve_cascade(page, target, cache) -> tuple[ResolvedElement, CascadeTrace]`
  - `REGISTERED_RUNGS: list[Rung]` — populated as Tasks 14–18 land; Task 41 asserts its ids are `[1,2,3,4,5]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/resolve/test_cascade.py
from selector_drift.resolve.cascade import resolve_cascade

def test_cascade_tries_rungs_in_order_and_records_every_attempt(fake_page):
    el, trace = resolve_cascade(fake_page, TARGET, EmptyCache())
    assert [a.rung_id for a in trace.attempts] == [1, 2]
    assert trace.winner == 2
    assert trace.attempts[0].reason

def test_healed_is_false_only_on_a_rung_one_win(fake_page_cache_hit):
    _, trace = resolve_cascade(fake_page_cache_hit, TARGET, WarmCache())
    assert trace.winner == 1 and trace.healed is False

def test_every_attempt_is_timed(fake_page):
    _, trace = resolve_cascade(fake_page, TARGET, EmptyCache())
    assert all(a.ms >= 0 for a in trace.attempts)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/resolve -v`
Expected: FAIL — `ModuleNotFoundError: selector_drift.resolve.cascade`.

- [ ] **Step 3: Implement the protocol, trace and runner.** The runner iterates `REGISTERED_RUNGS`, timing each, recording a `RungAttempt` for every one including failures, and stops at the first non-`None`.

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `uv run pytest tests/resolve -v && uv run mypy --strict selector_drift/resolve`
Expected: PASS, 3 tests. mypy clean.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/resolve tests/resolve
git commit -m "feat(resolve): Rung protocol, cascade trace, instrumented runner"
```

---

### Task 14: Rung 2 — the accessibility tree

**Files:**
- Create: `selector_drift/resolve/rungs/r2_axtree.py`
- Test: `tests/resolve/test_r2_axtree.py`

**Interfaces:**
- Consumes: `Rung`, `ResolvedElement`, `AnchorEvidence`, `Family` (Task 13).
- Produces: `AxTreeRung` with `rung_id = 2`, `family = Family.TEXT_SEMANTIC`. Populates `AnchorEvidence.accessible_name`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/resolve/test_r2_axtree.py
import pytest
from selector_drift.resolve.rungs.r2_axtree import AxTreeRung
from testbench.mutations.class_rename import ClassRename
from testbench.mutations.dom_restructure import DomRestructure
from testbench.mutations.id_churn import IdChurn
from testbench.mutations.table_to_div import TableToDiv
from testbench.mutations.label_reword import LabelReword

@pytest.mark.parametrize("mutation", [ClassRename(), DomRestructure(), IdChurn(), TableToDiv()])
def test_axtree_survives_structural_mutations(mutation, bench_page):
    el = AxTreeRung().resolve(bench_page.mutated(mutation), CLAIM_NUMBER)
    assert el is not None and el.anchor.accessible_name == "Claim Number"

def test_axtree_fails_on_label_reword(bench_page):
    assert AxTreeRung().resolve(bench_page.mutated(LabelReword()), CLAIM_NUMBER) is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/resolve/test_r2_axtree.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement using Playwright's `get_by_role` / ARIA snapshot,** matching on role + accessible name + relationship, and register it in `REGISTERED_RUNGS`.

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/resolve -v`
Expected: PASS, 5 new tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/resolve/rungs/r2_axtree.py tests/resolve/test_r2_axtree.py
git commit -m "feat(resolve): rung 2, accessibility tree"
```

---

### Task 15: Rung 3 — text anchors and structural relations

**Files:**
- Create: `selector_drift/resolve/rungs/r3_anchors.py`
- Test: `tests/resolve/test_r3_anchors.py`

**Interfaces:**
- Consumes: `Rung`, `AnchorEvidence`, `Family` (Task 13), `Relation` (Task 5).
- Produces: `AnchorRung`, `rung_id = 3`, `family = Family.TEXT_SEMANTIC`. Resolves "the value in the cell to the right of the label X". Populates `AnchorEvidence.label_text`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/resolve/test_r3_anchors.py
import pytest
from selector_drift.resolve.rungs.r3_anchors import AnchorRung
from testbench.mutations.class_rename import ClassRename
from testbench.mutations.id_churn import IdChurn
from testbench.mutations.framework_markup import FrameworkMarkup
from testbench.mutations.table_to_div import TableToDiv
from testbench.mutations.label_reword import LabelReword

@pytest.mark.parametrize("mutation", [ClassRename(), IdChurn(), FrameworkMarkup(), TableToDiv()])
def test_anchors_survive_restyling_and_markup_swaps(mutation, bench_page):
    el = AnchorRung().resolve(bench_page.mutated(mutation), CLAIM_NUMBER)
    assert el is not None and el.anchor.label_text == "Claim Number"

def test_anchors_fail_on_label_reword(bench_page):
    assert AnchorRung().resolve(bench_page.mutated(LabelReword()), CLAIM_NUMBER) is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/resolve/test_r3_anchors.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement** — locate the label text node, walk the declared `Relation` (`RIGHT_OF`, `BELOW`, `WITHIN`), return the target. Register in `REGISTERED_RUNGS`.

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/resolve -v`
Expected: PASS, 5 new tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/resolve/rungs/r3_anchors.py tests/resolve/test_r3_anchors.py
git commit -m "feat(resolve): rung 3, text anchors and relations"
```

---

### Task 16: Rung 4 — attribute and structural heuristics

**Files:**
- Create: `selector_drift/resolve/rungs/r4_heuristics.py`
- Test: `tests/resolve/test_r4_heuristics.py`

**Interfaces:**
- Consumes: `Rung`, `AnchorEvidence`, `Family` (Task 13).
- Produces: `HeuristicRung`, `rung_id = 4`, `family = Family.STRUCTURAL`. Tries `data-testid`, then stable id patterns, then **column index with header verification** — it reads the header cell at the resolved index and requires it to match the expected header. Populates `AnchorEvidence.header_text`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/resolve/test_r4_heuristics.py
from selector_drift.resolve.rungs.r4_heuristics import HeuristicRung
from testbench.mutations.label_reword import LabelReword
from testbench.mutations.class_rename import ClassRename
from testbench.mutations.column_reorder import ColumnReorder

def test_heuristics_heals_through_label_reword(bench_page):
    assert HeuristicRung().resolve(bench_page.mutated(LabelReword()), CLAIM_NUMBER) is not None

def test_heuristics_heals_through_restyling(bench_page):
    assert HeuristicRung().resolve(bench_page.mutated(ClassRename()), CLAIM_NUMBER) is not None

def test_heuristics_refuses_when_the_header_at_that_index_does_not_match(bench_page):
    """Header verification is what stops a bare column index being a guess."""
    assert HeuristicRung().resolve(bench_page.mutated(ColumnReorder()), CLAIM_NUMBER) is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/resolve/test_r4_heuristics.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement, header verification included** — a column index whose header does not match is never returned. Register in `REGISTERED_RUNGS`.

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/resolve -v`
Expected: PASS, 3 new tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/resolve/rungs/r4_heuristics.py tests/resolve/test_r4_heuristics.py
git commit -m "feat(resolve): rung 4, heuristics with header verification"
```

---

### Task 17: Rung 1 — the cache that verifies provenance, not just the selector

**Files:**
- Create: `selector_drift/resolve/rungs/r1_cache.py`, `selector_drift/resolve/cache.py`
- Test: `tests/resolve/test_r1_cache.py`

**Interfaces:**
- Consumes: `Rung`, `AnchorEvidence`, `Family` (Task 13), `selector_cache` table (Task 3).
- Produces:
  - `CachedSelector(selector: str, anchor: AnchorEvidence, set_by_rung: int)`
  - `SelectorCache.get(target: str, field: str) -> CachedSelector | None`
  - `SelectorCache.put(target: str, field: str, element: ResolvedElement) -> None` — **the entire write-back. Self-healing is this line and nothing else.**
  - `CacheRung(cache: SelectorCache)`, `rung_id = 1`, `family = Family.NONE`

**Rung 1 is built after rungs 2–4 on purpose:** the anchor evidence it stores is produced by whichever rung healed, so those must exist first.

- [ ] **Step 1: Write the failing tests — the most important tests in the repo**

```python
# tests/resolve/test_r1_cache.py
from selector_drift.resolve.rungs.r1_cache import CacheRung
from testbench.mutations.column_reorder import ColumnReorder

def test_cache_hit_requires_exactly_one_match(bench_page, warm_cache):
    page = bench_page.with_duplicate_matching_element()
    assert CacheRung(warm_cache).resolve(page, CLAIM_NUMBER) is None

def test_column_reorder_fails_rung_one_even_though_the_selector_still_matches(bench_page, warm_cache):
    """Spec 5.2. The selector still matches exactly one element and the value
    still satisfies the format assertion — but the anchor has moved, so rung 1
    MUST fail, so a heal is recorded, so the strict path in Task 20 engages.
    Without this, the adjacent-column failure is silent and permanent."""
    page = bench_page.mutated(ColumnReorder())
    cached = warm_cache.get("testbench", "claim_number")
    assert page.count_matching(cached.selector) == 1
    assert CacheRung(warm_cache).resolve(page, CLAIM_NUMBER) is None

def test_unchanged_page_is_a_cache_hit(bench_page, warm_cache):
    el = CacheRung(warm_cache).resolve(bench_page, CLAIM_NUMBER)
    assert el is not None and el.rung_id == 1

def test_put_stores_the_anchor_from_the_healing_rung(warm_cache, healed_element):
    warm_cache.put("testbench", "claim_number", healed_element)
    assert warm_cache.get("testbench", "claim_number").anchor == healed_element.anchor
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/resolve/test_r1_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: selector_drift.resolve.cache`.

- [ ] **Step 3: Implement the cache and the rung**

`CacheRung.resolve()` returns `None` unless **all three** hold: the selector matches exactly one element; the stored `AnchorEvidence` still holds against that element (whichever of accessible name / label text / header text was recorded); and the element satisfies the target's format assertions. Register in `REGISTERED_RUNGS` at position 0.

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `uv run pytest tests/resolve -v`
Expected: PASS, 4 new tests, all rung tests green.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/resolve/rungs/r1_cache.py selector_drift/resolve/cache.py tests/resolve/test_r1_cache.py
git commit -m "feat(resolve): rung 1 verifies anchor provenance, not just the selector"
```

---

### Task 18: Rung 5 — escalation, and the corpus it builds

**Files:**
- Create: `selector_drift/resolve/rungs/r5_visual.py`, `selector_drift/resolve/escalation.py`
- Test: `tests/resolve/test_r5_escalation.py`

**Interfaces:**
- Consumes: `Rung`, `Family` (Task 13).
- Produces: `VisualRung(corpus: Path)`, `rung_id = 5`, `family = Family.NONE`; `EscalateToHuman(Exception)`. Records the case with full evidence into `corpus/` and raises. **Makes no model call and imports nothing from `bok_core.gateway`.**

- [ ] **Step 1: Write the failing tests**

```python
# tests/resolve/test_r5_escalation.py
import subprocess, pytest
from selector_drift.resolve.rungs.r5_visual import VisualRung
from selector_drift.resolve.escalation import EscalateToHuman

def test_rung_five_escalates_and_never_returns_an_element(unresolvable_page, tmp_path):
    with pytest.raises(EscalateToHuman):
        VisualRung(corpus=tmp_path).resolve(unresolvable_page, CLAIM_NUMBER)

def test_rung_five_records_a_corpus_case(unresolvable_page, tmp_path):
    with pytest.raises(EscalateToHuman):
        VisualRung(corpus=tmp_path).resolve(unresolvable_page, CLAIM_NUMBER)
    assert list(tmp_path.glob("*.json"))

def test_no_module_imports_the_gateway():
    assert subprocess.run(["uv", "run", "lint-imports"]).returncode == 0
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/resolve/test_r5_escalation.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement escalation and corpus recording.** Register in `REGISTERED_RUNGS` last.

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/resolve -v && uv run lint-imports`
Expected: PASS, 3 new tests. `no-model-calls` kept.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/resolve/rungs/r5_visual.py selector_drift/resolve/escalation.py tests/resolve/test_r5_escalation.py
git commit -m "feat(resolve): rung 5 escalates to human review, builds eval corpus"
```

---

### Task 19: Capture and replay — the regression harness the mutation suite reuses

**Files:**
- Create: `selector_drift/heallog/capture.py`, `selector_drift/heallog/replay.py`
- Test: `tests/heallog/test_replay.py`

**Interfaces:**
- Consumes: `PageHandle` (Task 4), `resolve_cascade` (Task 13).
- Produces:
  - `Evidence(dom: str, screenshot: bytes, captured_at: datetime, sha256: str)`
  - `capture(page: PageHandle) -> Evidence`
  - `replay(evidence: Evidence, target: FieldTarget, cache) -> tuple[ResolvedElement, CascadeTrace]` — runs the cascade against the **stored DOM**, never a live page
  - `NotReproducible(Exception)`

`heallog/` cannot import `browser/` (Task 1 contract), so replay-from-stored-evidence is structural, not a convention.

- [ ] **Step 1: Write the failing tests**

```python
# tests/heallog/test_replay.py
from dataclasses import replace
import pytest, subprocess
from selector_drift.heallog.replay import replay, NotReproducible

def test_replay_reproduces_the_same_rung_and_element(captured_heal):
    el, trace = replay(captured_heal.evidence, CLAIM_NUMBER, captured_heal.cache)
    assert trace.winner == captured_heal.winning_rung
    assert el.selector == captured_heal.selector_after

def test_corrupted_evidence_is_reported_not_silently_counted(captured_heal):
    corrupted = replace(captured_heal.evidence, dom="<html></html>")
    with pytest.raises(NotReproducible):
        replay(corrupted, CLAIM_NUMBER, captured_heal.cache)

def test_heallog_cannot_import_browser():
    assert subprocess.run(["uv", "run", "lint-imports"]).returncode == 0
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/heallog -v`
Expected: FAIL — `ModuleNotFoundError: selector_drift.heallog.replay`.

- [ ] **Step 3: Implement capture and replay.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/heallog -v && uv run lint-imports`
Expected: PASS, 3 tests. `heallog-cannot-refetch` kept.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/heallog/capture.py selector_drift/heallog/replay.py tests/heallog
git commit -m "feat(heallog): capture and replay, the shared regression harness"
```

---

# Phase D — The gate (weeks 16–17). This is the product.

---

### Task 20: `Verdict`, the `Assertion` protocol, and the asymmetry

**Files:**
- Create: `selector_drift/validate/verdict.py`, `selector_drift/validate/assertions/base.py`, `selector_drift/validate/gate.py`
- Test: `tests/validate/test_gate_asymmetry.py`

**Interfaces:**
- Consumes: `CascadeTrace` (Task 13).
- Produces:
  - `Outcome` — `StrEnum` `PASS`, `FAIL`, `INSUFFICIENT`
  - `Verdict(check: str, outcome: Outcome, threshold: str | None, observed: str | None, reason: str)` — **never a bare boolean**; the UI has nothing else it could render, which is how spec §8 screen 5's "no bare red badges" becomes structural
  - `Assertion` protocol — `name: str`, `check(candidate, history, ctx) -> Verdict`
  - `HealContext(healed: bool, winning_rung: int, page, target, cache, human_confirmed: bool = False)`
  - `gate(candidate: CandidateExtraction, history, ctx: HealContext, checks: list[Assertion]) -> ValidatedExtraction | QuarantinedRun`
  - `ValidatedExtraction(rows, run_key)` — **frozen, constructor requires a module-private `_ValidateToken`, minted only inside `gate.py`**

- [ ] **Step 1: Write the failing tests — the asymmetry is the whole point**

```python
# tests/validate/test_gate_asymmetry.py
import pytest
from selector_drift.validate.gate import gate, HealContext, ValidatedExtraction
from selector_drift.validate.quarantine import QuarantinedRun

def test_insufficient_is_tolerated_on_a_cache_hit():
    ctx = HealContext(healed=False, winning_rung=1, page=P, target=T, cache=C)
    assert isinstance(gate(CANDIDATE, HISTORY, ctx, [AlwaysInsufficient()]), ValidatedExtraction)

def test_insufficient_is_a_failure_on_a_heal():
    """Spec 5.7. When the selector changed we demand full evidence; when nothing
    changed we do not. This is what stops the distributional check crying wolf."""
    ctx = HealContext(healed=True, winning_rung=3, page=P, target=T, cache=C)
    assert isinstance(gate(CANDIDATE, HISTORY, ctx, [AlwaysInsufficient()]), QuarantinedRun)

@pytest.mark.parametrize("healed,rung", [(False, 1), (True, 3)])
def test_any_fail_quarantines_regardless_of_heal(healed, rung):
    ctx = HealContext(healed=healed, winning_rung=rung, page=P, target=T, cache=C)
    assert isinstance(gate(CANDIDATE, HISTORY, ctx, [AlwaysFail()]), QuarantinedRun)

def test_validated_extraction_cannot_be_built_outside_validate():
    with pytest.raises(TypeError):
        ValidatedExtraction(rows=[], run_key="x")  # type: ignore[call-arg]

def test_a_quarantined_run_carries_every_verdict():
    ctx = HealContext(healed=True, winning_rung=3, page=P, target=T, cache=C)
    q = gate(CANDIDATE, HISTORY, ctx, [AlwaysFail(), AlwaysPass()])
    assert len(q.verdicts) == 2
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/validate/test_gate_asymmetry.py -v`
Expected: FAIL — `ModuleNotFoundError: selector_drift.validate.verdict`.

- [ ] **Step 3: Implement `Verdict`, the protocol, the token-gated `ValidatedExtraction`, and `gate()`**

```python
def _effective(v: Verdict, healed: bool) -> Outcome:
    if v.outcome is Outcome.INSUFFICIENT and healed:
        return Outcome.FAIL
    return v.outcome
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `uv run pytest tests/validate -v && uv run mypy --strict selector_drift/validate`
Expected: PASS, 6 tests. mypy clean.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/validate tests/validate
git commit -m "feat(validate): Verdict, gate, and the heal asymmetry"
```

---

### Task 21: Format, domain and cross-field assertions

**Files:**
- Create: `selector_drift/validate/assertions/format.py`, `domain.py`, `cross_field.py`
- Test: `tests/validate/test_basic_assertions.py`

**Interfaces:**
- Consumes: `Assertion`, `Verdict`, `Outcome` (Task 20), `AssertionSpec` (Task 5).
- Produces: `FormatAssertion(pattern: str)`, `DomainAssertion(rule: DomainRule)`, `CrossFieldAssertion(expression: str)`. All three work on run one with no history.

- [ ] **Step 1: Write the failing tests**

```python
# tests/validate/test_basic_assertions.py
from selector_drift.validate.assertions.format import FormatAssertion
from selector_drift.validate.assertions.domain import DomainAssertion
from selector_drift.validate.assertions.cross_field import CrossFieldAssertion
from selector_drift.validate.verdict import Outcome

def test_format_assertion_reports_its_threshold_in_the_verdict():
    v = FormatAssertion(r"^[A-Z]{2}\d{8}$").check(rows(["AB12345678"]), None, CTX)
    assert v.outcome is Outcome.PASS and v.threshold == r"^[A-Z]{2}\d{8}$"

def test_format_assertion_fails_and_names_the_offending_value():
    v = FormatAssertion(r"^[A-Z]{2}\d{8}$").check(rows(["12345678"]), None, CTX)
    assert v.outcome is Outcome.FAIL and "12345678" in v.observed

def test_domain_assertion_catches_an_out_of_range_amount():
    v = DomainAssertion({"min": 0, "max": 1_000_000}).check(rows([2_000_000]), None, CTX)
    assert v.outcome is Outcome.FAIL

def test_domain_assertion_catches_an_implausible_date():
    v = DomainAssertion({"date_after": "2000-01-01"}).check(rows(["1899-01-01"]), None, CTX)
    assert v.outcome is Outcome.FAIL

def test_cross_field_catches_a_row_that_no_longer_foots():
    v = CrossFieldAssertion("net + tax == gross").check(
        rows([{"net": 100, "tax": 20, "gross": 200}]), None, CTX)
    assert v.outcome is Outcome.FAIL and "foot" in v.reason.lower()

def test_cross_field_passes_on_a_row_that_foots():
    v = CrossFieldAssertion("net + tax == gross").check(
        rows([{"net": 100, "tax": 20, "gross": 120}]), None, CTX)
    assert v.outcome is Outcome.PASS
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/validate/test_basic_assertions.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the three assertions,** each populating `threshold`, `observed` and `reason` on every verdict it returns.

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/validate -v`
Expected: PASS, 6 new tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/validate/assertions tests/validate/test_basic_assertions.py
git commit -m "feat(validate): format, domain and cross-field assertions"
```

---

### Task 22: `IndependentRungAgreement` — cross-family confirmation

**Files:**
- Create: `selector_drift/validate/assertions/independence.py`
- Test: `tests/validate/test_independence.py`

**Interfaces:**
- Consumes: the `Rung` protocol and `Family` **only** — never a concrete rung module (Task 1 contract).
- Produces: `IndependentRungAgreement(rungs: list[Rung])`. Re-resolves the element with a rung from a **different `Family`** than the one that healed, and requires the two to point at the same node.

| Healed by | Valid confirmer |
|---|---|
| 2 or 3 (`TEXT_SEMANTIC`) | 4 (`STRUCTURAL`) |
| 4 (`STRUCTURAL`) | 2 or 3 (`TEXT_SEMANTIC`) |
| 1 | n/a — never a valid confirmer; it is what broke |

- [ ] **Step 1: Write the failing tests**

```python
# tests/validate/test_independence.py
from selector_drift.validate.assertions.independence import IndependentRungAgreement
from selector_drift.validate.verdict import Outcome
from selector_drift.validate.gate import HealContext
from selector_drift.resolve.rungs.r2_axtree import AxTreeRung
from selector_drift.resolve.rungs.r3_anchors import AnchorRung
from selector_drift.resolve.rungs.r4_heuristics import HeuristicRung

def ctx(rung): return HealContext(healed=True, winning_rung=rung, page=P, target=T, cache=C)

def test_same_family_confirmer_is_rejected():
    """Rungs 2 and 3 both key off text, so a label reword fools both identically.
    Their agreement proves nothing and must not be accepted as confirmation."""
    v = IndependentRungAgreement([AxTreeRung(), AnchorRung()]).check(CANDIDATE, None, ctx(3))
    assert v.outcome is Outcome.INSUFFICIENT and "cross-family" in v.reason

def test_cross_family_agreement_passes():
    v = IndependentRungAgreement([AxTreeRung(), HeuristicRung()]).check(CANDIDATE, None, ctx(3))
    assert v.outcome is Outcome.PASS and v.observed == "rung 4"

def test_cross_family_disagreement_fails():
    """The decoy column: rung 4 healed to Prior Amount, rung 2 points at Amount."""
    v = IndependentRungAgreement([AxTreeRung(), HeuristicRung()]).check(DECOY_CANDIDATE, None, ctx(4))
    assert v.outcome is Outcome.FAIL

def test_no_confirmer_available_is_insufficient_never_pass():
    v = IndependentRungAgreement([HeuristicRung()]).check(CANDIDATE, None, ctx(4))
    assert v.outcome is Outcome.INSUFFICIENT

def test_rung_one_is_never_used_as_a_confirmer():
    v = IndependentRungAgreement([CacheRung(C), HeuristicRung()]).check(CANDIDATE, None, ctx(4))
    assert v.observed != "rung 1"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/validate/test_independence.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement cross-family confirmation.** Select candidate confirmers where `family != healed_rung.family and family is not Family.NONE`. If none resolves, return `INSUFFICIENT` — never `PASS`.

- [ ] **Step 4: Run the tests and confirm the contract holds**

Run: `uv run pytest tests/validate -v && uv run lint-imports`
Expected: PASS, 5 new tests. `judged-cannot-call-judge` kept.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/validate/assertions/independence.py tests/validate/test_independence.py
git commit -m "feat(validate): cross-family independent rung agreement"
```

---

### Task 23: `DistributionalAssertion` — PSI, warm-up, persistence

**Files:**
- Create: `selector_drift/validate/assertions/distribution.py`
- Test: `tests/validate/test_distribution.py`

**Interfaces:**
- Consumes: `Assertion`, `Verdict` (Task 20), run history from Postgres.
- Produces: `DistributionalAssertion(threshold: float = 0.20, warmup_runs: int = 30, persistence: int = 2)`. Returns `INSUFFICIENT` while `len(history) < warmup_runs`; `FAIL` only when PSI exceeds the threshold on `persistence` consecutive runs; `PASS` when the client has marked the shift expected.

- [ ] **Step 1: Write the failing tests**

```python
# tests/validate/test_distribution.py
from selector_drift.validate.assertions.distribution import DistributionalAssertion
from selector_drift.validate.verdict import Outcome

def test_warmup_returns_insufficient_not_pass():
    v = DistributionalAssertion().check(CANDIDATE, history(n=5), CTX)
    assert v.outcome is Outcome.INSUFFICIENT and "warm-up" in v.reason

def test_synthetic_shift_is_detected_after_warmup():
    v = DistributionalAssertion().check(shifted(), history(n=40, consecutive_shifts=2), CTX)
    assert v.outcome is Outcome.FAIL and float(v.observed) > 0.20

def test_seasonality_does_not_alert_before_persistence_is_met():
    """B7. One shifted run is month-end. Two in a row is a problem."""
    v = DistributionalAssertion().check(shifted(), history(n=40, consecutive_shifts=1), CTX)
    assert v.outcome is Outcome.PASS

def test_client_marked_expected_suppresses_the_alert():
    v = DistributionalAssertion().check(shifted(), history(n=40, marked_expected=True), CTX)
    assert v.outcome is Outcome.PASS

def test_the_verdict_always_states_the_threshold():
    v = DistributionalAssertion().check(CANDIDATE, history(n=40), CTX)
    assert v.threshold == "PSI 0.20"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/validate/test_distribution.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement PSI with the documented threshold, warm-up and persistence rules.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/validate -v`
Expected: PASS, 5 new tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/validate/assertions/distribution.py tests/validate/test_distribution.py
git commit -m "feat(validate): distributional assertion with warm-up and persistence"
```

---

### Task 24: `FirstHealConfirmation` and the scope-record policy

**Files:**
- Create: `selector_drift/validate/assertions/first_heal.py`
- Test: `tests/validate/test_first_heal.py`

**Interfaces:**
- Consumes: `AuthorisedTarget.first_heal_policy` (Task 2), `SelectorCache` (Task 17), `HealContext.human_confirmed` (Task 20).
- Produces: `FirstHealConfirmation()`. `INSUFFICIENT` when a heal targets a selector never seen before, the policy is `REQUIRED`, and `human_confirmed` is `False`; `PASS` when the policy is `AUTO_PROMOTE`, when the selector is already known, or when `human_confirmed` is `True`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/validate/test_first_heal.py
from selector_drift.validate.assertions.first_heal import FirstHealConfirmation
from selector_drift.validate.verdict import Outcome

def test_first_heal_to_a_new_selector_requires_a_human_by_default():
    v = FirstHealConfirmation().check(CANDIDATE, None, ctx(policy="required", seen=False))
    assert v.outcome is Outcome.INSUFFICIENT

def test_repeat_heal_to_a_known_selector_runs_automatically():
    v = FirstHealConfirmation().check(CANDIDATE, None, ctx(policy="required", seen=True))
    assert v.outcome is Outcome.PASS

def test_auto_promote_is_permitted_only_where_the_scope_record_says_so():
    """Spec 5.8. The bench sets auto_promote because it is synthetic with a
    known answer key. Putting the policy on the scope record means it cannot be
    quietly disabled for a client target."""
    v = FirstHealConfirmation().check(CANDIDATE, None, ctx(policy="auto_promote", seen=False))
    assert v.outcome is Outcome.PASS

def test_human_confirmation_satisfies_the_check():
    v = FirstHealConfirmation().check(CANDIDATE, None, ctx(policy="required", seen=False, confirmed=True))
    assert v.outcome is Outcome.PASS
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/validate/test_first_heal.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/validate -v`
Expected: PASS, 4 new tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/validate/assertions/first_heal.py tests/validate/test_first_heal.py
git commit -m "feat(validate): first-heal confirmation driven by the scope record"
```

---

### Task 25: Quarantine, promote, discard — and promotion re-runs the gate

**Files:**
- Create: `selector_drift/validate/quarantine.py`
- Test: `tests/validate/test_quarantine.py`

**Interfaces:**
- Consumes: `gate` (Task 20), `quarantine` and `audit_log` tables (Task 3).
- Produces:
  - `QuarantinedRun(run_key: str, reason: str, verdicts: list[Verdict], data_uri: str)`
  - `promote(run_key: str, actor: str, reason: str) -> ValidatedExtraction` — **re-runs `gate()`** with `human_confirmed=True` in the context. It supplies the missing evidence; it does not skip a check.
  - `discard(run_key: str, actor: str, reason: str) -> None`
  - `StillInvalid(Exception)` — raised when a re-run gate still fails
  - Both write an `audit_log` row.

- [ ] **Step 1: Write the failing tests**

```python
# tests/validate/test_quarantine.py
import pytest
from selector_drift.validate.quarantine import promote, discard, StillInvalid
from selector_drift import land

def test_a_quarantined_run_never_reaches_land(quarantined):
    with pytest.raises(TypeError):
        land.write(quarantined)  # type: ignore[arg-type]

def test_promotion_re_runs_the_gate_rather_than_bypassing_it(quarantined, monkeypatch):
    calls = []
    real = gate
    monkeypatch.setattr("selector_drift.validate.gate.gate",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    promote(quarantined.run_key, actor="me", reason="checked the screenshots")
    assert calls == [1]

def test_promotion_still_fails_if_a_check_still_fails(quarantined_with_format_failure):
    with pytest.raises(StillInvalid):
        promote(quarantined_with_format_failure.run_key, actor="me", reason="looks fine")

def test_promotion_is_audited(quarantined):
    promote(quarantined.run_key, actor="me", reason="confirmed")
    row = latest_audit()
    assert row.actor == "me" and row.action == "promote" and row.detail["reason"] == "confirmed"

def test_discard_is_audited_and_writes_nothing(quarantined):
    discard(quarantined.run_key, actor="me", reason="wrong column")
    assert latest_audit().action == "discard"
    assert rows_written_for(quarantined.run_key) == 0
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/validate/test_quarantine.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement quarantine, promote and discard.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/validate -v`
Expected: PASS, 5 new tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/validate/quarantine.py tests/validate/test_quarantine.py
git commit -m "feat(validate): quarantine with audited promotion that re-runs the gate"
```

---

### Task 26: The two suites — every mutation heals, every adversarial mutation is refused

**Files:**
- Create: `tests/suite/conftest.py` — captures one `Evidence` per mutation class via Task 19
- Create: `tests/suite/test_mutation_heals.py`, `tests/suite/test_adversarial_refused.py`

**Interfaces:**
- Consumes: every rung, `gate`, `replay`, all nine mutations. Imports `testbench.answers.key` — **permitted in tests, forbidden in `selector_drift/`.**
- Produces: the regression suite the resolver is measured against for the rest of the project.

- [ ] **Step 1: Write the failing heal suite**

```python
# tests/suite/test_mutation_heals.py
import pytest
from selector_drift.heallog.replay import replay
from testbench.answers.key import expected_selector

@pytest.mark.parametrize("mutation", ALL_EIGHT_MUTATION_CLASSES, ids=lambda m: m.name)
def test_every_mutation_class_heals_to_the_right_element(mutation, bench_evidence):
    ev = bench_evidence[mutation.name]
    el, trace = replay(ev, CLAIM_NUMBER, warm_cache())
    assert el.selector == expected_selector(ev.day, "claim_number")
    assert trace.winner in (2, 3, 4), "rungs 1-4 must carry this; rung 5 escalates"
```

- [ ] **Step 2: Write the failing adversarial suite — this set matters more**

```python
# tests/suite/test_adversarial_refused.py
from selector_drift.validate.gate import gate, HealContext
from selector_drift.validate.quarantine import QuarantinedRun
from selector_drift.validate.verdict import Outcome

def test_decoy_column_is_refused_by_the_gate(decoy_evidence):
    """The plausible-but-wrong heal. Every value looks valid. Only cross-family
    disagreement catches it, and it must catch it on run one with no history."""
    el, trace = replay(decoy_evidence, AMOUNT, warm_cache())
    ctx = HealContext(healed=True, winning_rung=trace.winner, page=None, target=AMOUNT, cache=warm_cache())
    result = gate(extract(el), history=None, ctx=ctx, checks=ALL_SIX_CHECKS)
    assert isinstance(result, QuarantinedRun)
    assert any(v.check == "IndependentRungAgreement" and v.outcome is Outcome.FAIL
               for v in result.verdicts)

def test_no_adversarial_mutation_ever_writes(decoy_evidence):
    assert rows_written_for(decoy_evidence.run_key) == 0
```

- [ ] **Step 3: Run both and watch them fail**

Run: `uv run pytest tests/suite -v`
Expected: FAIL.

- [ ] **Step 4: Fix the resolver and the gate until both suites pass.** If a mutation class cannot be healed by rungs 1–4, that is a resolver defect to fix here — **not** a reason to revive rung 5 (spec §5.3).

- [ ] **Step 5: Run the full check**

Run: *the full check*
Expected: PASS, coverage ≥ 75%.

- [ ] **Step 6: Record the rung distribution in `docs/methodology.md`**

```bash
uv run python -m selector_drift.orchestrate.rung_stats
```

Expected output shape: `rung1 82% · rung2 9% · rung3 6% · rung4 3% · rung5 0.0%`. **The rung-5 figure is a published claim (B11); it must come from this command, not from an estimate.**

- [ ] **Step 7: Commit**

```bash
git add tests/suite docs/methodology.md
git commit -m "test: mutation heal suite and adversarial refusal suite"
```

---

# Phase E — Extract, land, quality (weeks 17–18)

---

### Task 27: Typed extraction into `Claim`s

**Files:**
- Create: `selector_drift/extract/reader.py`, `selector_drift/extract/models.py`
- Test: `tests/extract/test_reader.py`

**Interfaces:**
- Consumes: `ResolvedElement` (Task 13), `TaskSpec` (Task 5), `bok_core.provenance.Claim`, `Method`.
- Produces:
  - `CandidateExtraction(rows: list[dict[str, Claim]], trace: CascadeTrace, spec_version: int, run_key: str)`
  - `read(element: ResolvedElement, spec: TaskSpec) -> CandidateExtraction`

Every value is a `Claim` with `method=DETERMINISTIC` and therefore `confidence=None` — the honest encoding, which makes `ConfidenceMeter` render as uncalibrated (`00` B10).

- [ ] **Step 1: Write the failing tests**

```python
# tests/extract/test_reader.py
from bok_core.provenance import Method
from selector_drift.extract.reader import read

def test_every_extracted_value_is_a_deterministic_claim(resolved):
    for row in read(resolved, SPEC).rows:
        for claim in row.values():
            assert claim.method is Method.DETERMINISTIC and claim.confidence is None

def test_claim_source_points_at_the_selector_that_produced_it(resolved):
    claim = read(resolved, SPEC).rows[0]["claim_number"]
    assert resolved.selector in str(claim.source)

def test_candidate_carries_the_run_key_and_spec_version(resolved):
    c = read(resolved, SPEC)
    assert c.run_key and c.spec_version == SPEC.version
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/extract -v`
Expected: FAIL.

- [ ] **Step 3: Implement `read()`.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/extract -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/extract tests/extract
git commit -m "feat(extract): typed extraction into deterministic Claims"
```

---

### Task 28: The landing zone, idempotent under replay

**Files:**
- Create: `selector_drift/land/writer.py`, `selector_drift/land/catalog.py`, `selector_drift/land/compact.py`
- Test: `tests/land/test_idempotency.py`

**Interfaces:**
- Consumes: `ValidatedExtraction` (Task 20), `run_key` (Task 6).
- Produces:
  - `write(validated: ValidatedExtraction) -> Path` — **accepts no other type**; one Parquet file per run into `lake/target=<t>/window=<w>/`
  - `query(sql: str)` — DuckDB over the partitioned tree, **never a shared file written by parallel flows**
  - `compact(target: str) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/land/test_idempotency.py
import pytest
from selector_drift.land.writer import write
from selector_drift.land.catalog import query

def test_write_rejects_an_unvalidated_candidate(candidate):
    with pytest.raises(TypeError):
        write(candidate)  # type: ignore[arg-type]

def test_replaying_a_run_does_not_duplicate_rows(validated):
    write(validated); write(validated)
    assert query("select count(*) from lake").fetchone()[0] == len(validated.rows)

def test_a_late_record_does_not_corrupt_a_closed_partition(validated, late_row):
    write(validated); write(with_row(validated, late_row))
    n = query("select count(*) from lake where window='2026-09-07'").fetchone()[0]
    assert n == len(validated.rows) + 1

def test_no_shared_duckdb_file_is_ever_created(lake_dir):
    assert not list(lake_dir.rglob("*.duckdb"))

def test_compaction_reduces_file_count_without_changing_row_count(validated, lake_dir):
    for _ in range(5): write(with_new_window(validated))
    before = query("select count(*) from lake").fetchone()[0]
    compact("testbench")
    assert query("select count(*) from lake").fetchone()[0] == before
    assert len(list(lake_dir.rglob("*.parquet"))) < 5
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/land -v`
Expected: FAIL.

- [ ] **Step 3: Implement the writer, catalog and compaction** with upsert on the natural key.

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/land -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/land tests/land
git commit -m "feat(land): idempotent Parquet lake with DuckDB query layer"
```

---

### Task 29: Data-quality contracts

**Files:**
- Create: `selector_drift/quality/contracts.py`
- Test: `tests/quality/test_contracts.py`

**Interfaces:**
- Consumes: `land.query` (Task 28), `bok_core.findings.Finding`, `Severity`.
- Produces: `evaluate(run_key: str) -> list[Finding]` covering row count vs expectation, null rate, schema drift and distribution shift. Every `Finding.detail` carries `threshold` and `threshold_reason`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/quality/test_contracts.py
from selector_drift.quality.contracts import evaluate

def test_row_count_shortfall_is_reported():
    fs = evaluate(run_with_half_the_expected_rows())
    assert any(f.rule == "row_count" for f in fs)

def test_null_rate_spike_is_reported():
    assert any(f.rule == "null_rate" for f in evaluate(run_with_null_spike()))

def test_schema_drift_is_reported():
    assert any(f.rule == "schema_drift" for f in evaluate(run_with_new_column()))

def test_every_finding_states_its_threshold_and_why():
    for f in evaluate(run_with_half_the_expected_rows()):
        assert f.detail["threshold"] and f.detail["threshold_reason"]

def test_a_clean_run_produces_no_findings():
    assert evaluate(clean_run()) == []
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/quality -v`
Expected: FAIL.

- [ ] **Step 3: Implement, reusing the shared severity ladder and the SARIF emitter.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/quality -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/quality tests/quality
git commit -m "feat(quality): data-quality contracts as typed Findings"
```

---

### Task 30: The change-set export — the answer to "can you write back?"

**Files:**
- Create: `selector_drift/land/changeset.py`
- Test: `tests/land/test_changeset.py`

**Interfaces:**
- Consumes: `land.query` (Task 28).
- Produces: `changeset(run_key: str, prior: Path, fmt: Literal["csv", "json"]) -> Path` — rows plus a diff against a client-supplied prior snapshot, each row carrying `action` (`add`/`change`/`unchanged`) and an `accept` flag defaulting to `false`. **No connectors. We never hold write credentials.**

- [ ] **Step 1: Write the failing tests**

```python
# tests/land/test_changeset.py
from pathlib import Path
from selector_drift.land.changeset import changeset

def test_changeset_marks_every_row_and_defaults_to_not_accepted(run_with_changes):
    rows = read_csv(changeset(run_with_changes, PRIOR, "csv"))
    assert {r["action"] for r in rows} <= {"add", "change", "unchanged"}
    assert all(r["accept"] == "false" for r in rows)

def test_a_changed_row_reports_both_values(run_with_changes):
    row = next(r for r in read_csv(changeset(run_with_changes, PRIOR, "csv")) if r["action"] == "change")
    assert row["prior_value"] and row["new_value"] and row["prior_value"] != row["new_value"]

def test_changeset_opens_no_network_connection():
    src = Path("selector_drift/land/changeset.py").read_text()
    assert "requests" not in src and "httpx" not in src and "urllib" not in src
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/land/test_changeset.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the diff and export.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/land -v`
Expected: PASS, 3 new tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/land/changeset.py tests/land/test_changeset.py
git commit -m "feat(land): reviewable change-set export"
```

---

# Phase F — Credentials and evidence (week 18)

---

### Task 31: `CredentialProvider`, session injection, and the seed that does not exist

**Files:**
- Create: `selector_drift/credentials/provider.py`, `selector_drift/credentials/session.py`
- Test: `tests/credentials/test_no_totp_seed.py`, `tests/credentials/test_session.py`

**Interfaces:**
- Consumes: `AuthorisedTarget` (Task 2).
- Produces:
  - `Credentials(cookies: list[dict] | None, username: str | None, password: SecretStr | None)`
  - `CredentialProvider` protocol — `get(target: AuthorisedTarget) -> Credentials`, `get_totp_code(target: AuthorisedTarget) -> str`
  - `NotSupported(Exception)` — raised by `get_totp_code` on every provider except `ClientBroker`
  - `SessionInjection(storage_state_path: Path)` — the **default offer**

- [ ] **Step 1: Write the failing tests — the guarantee you actually sell**

```python
# tests/credentials/test_no_totp_seed.py
import inspect, pkgutil, importlib
from pathlib import Path
import selector_drift

FORBIDDEN = {"totp_seed", "totp_secret", "otp_seed", "shared_secret", "seed"}

def test_no_field_anywhere_is_named_for_a_totp_seed():
    """Spec 2.1 Q2. Storing a TOTP seed defeats the second factor. This is not a
    policy we follow, it is a feature that does not exist."""
    for mod in pkgutil.walk_packages(selector_drift.__path__, "selector_drift."):
        m = importlib.import_module(mod.name)
        for _, cls in inspect.getmembers(m, inspect.isclass):
            fields = set(getattr(cls, "model_fields", {}))
            assert not (FORBIDDEN & fields), f"{cls.__name__} carries a seed field"

def test_totp_code_is_fetched_never_computed():
    assert "pyotp" not in Path("pyproject.toml").read_text()
```

```python
# tests/credentials/test_session.py
from selector_drift.credentials.provider import Credentials, NotSupported
from selector_drift.credentials.session import SessionInjection
import pytest

def test_password_is_a_secret_str():
    c = Credentials(password="hunter2")
    assert c.password.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(c)

def test_secrets_never_appear_in_a_log(caplog, target):
    SessionInjection(STATE_PATH).get(target)
    assert "hunter2" not in caplog.text

def test_session_injection_cannot_produce_a_totp_code(target):
    with pytest.raises(NotSupported):
        SessionInjection(STATE_PATH).get_totp_code(target)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/credentials -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the protocol and `SessionInjection`.**

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `uv run pytest tests/credentials -v && uv run gitleaks detect --no-git`
Expected: PASS, 5 tests. gitleaks clean.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/credentials tests/credentials
git commit -m "feat(credentials): provider protocol, session injection, no TOTP seed"
```

---

### Task 32: The client broker and the encrypted store

**Files:**
- Create: `selector_drift/credentials/broker.py`, `selector_drift/credentials/store.py`
- Modify: `docs/security.md`
- Test: `tests/credentials/test_broker.py`, `tests/credentials/test_store.py`

**Interfaces:**
- Consumes: `CredentialProvider`, `Credentials` (Task 31).
- Produces:
  - `ClientBroker(endpoint: str, token: SecretStr)` — the only provider implementing `get_totp_code()`, which **asks the client's endpoint for a fresh code**; the seed stays with them
  - `EncryptedStore(key_id: str)` — envelope encryption, per-client data key, ciphertext at rest, last resort

- [ ] **Step 1: Write the failing tests**

```python
# tests/credentials/test_broker.py
from selector_drift.credentials.broker import ClientBroker

def test_broker_fetches_a_code_and_stores_nothing(httpx_mock, tmp_path, target):
    httpx_mock.add_response(json={"code": "123456"})
    assert ClientBroker(ENDPOINT, TOKEN).get_totp_code(target) == "123456"
    assert not list(tmp_path.rglob("*"))

def test_broker_token_is_never_logged(caplog, httpx_mock, target):
    httpx_mock.add_response(json={"username": "u", "password": "p"})
    ClientBroker(ENDPOINT, TOKEN).get(target)
    assert TOKEN.get_secret_value() not in caplog.text
```

```python
# tests/credentials/test_store.py
from selector_drift.credentials.store import EncryptedStore
from selector_drift.credentials.provider import Credentials

def test_store_holds_no_plaintext_at_rest(tmp_path, target):
    EncryptedStore("client-a", root=tmp_path).put(target, Credentials(password="hunter2"))
    assert not any(b"hunter2" in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())

def test_each_client_gets_a_distinct_data_key(tmp_path):
    a = EncryptedStore("a", root=tmp_path)._data_key()
    b = EncryptedStore("b", root=tmp_path)._data_key()
    assert a != b

def test_round_trip_recovers_the_credential(tmp_path, target):
    s = EncryptedStore("client-a", root=tmp_path)
    s.put(target, Credentials(password="hunter2"))
    assert s.get(target).password.get_secret_value() == "hunter2"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/credentials -v`
Expected: FAIL.

- [ ] **Step 3: Implement both providers.**

- [ ] **Step 4: Document the posture in `docs/security.md`** — the three tiers, the preference order, and the seed refusal in plain language, including the sentence you will say in a first call.

- [ ] **Step 5: Run them and make sure they pass**

Run: `uv run pytest tests/credentials -v && uv run gitleaks detect --no-git`
Expected: PASS, 5 new tests. gitleaks clean.

- [ ] **Step 6: Commit**

```bash
git add selector_drift/credentials tests/credentials docs/security.md
git commit -m "feat(credentials): client broker and envelope-encrypted store"
```

---

### Task 33: Evidence storage — capture, redact, verify, store

**Files:**
- Create: `selector_drift/heallog/evidence.py`, `selector_drift/heallog/diff.py`
- Test: `tests/heallog/test_redaction_order.py`, `tests/heallog/test_diff.py`

**Interfaces:**
- Consumes: `Evidence` (Task 19), `bok_core.boundary.redact`, `Sensitivity`.
- Produces:
  - `store(evidence: Evidence, sensitivity: Sensitivity) -> str` — returns an `evidence_uri`. Order is **capture → redact → verify → store**; on verification failure it raises `RedactionFailed`, writes nothing, and the caller quarantines the run with reason `evidence redaction failed`.
  - `RedactionFailed(Exception)`
  - `dom_diff(before: str, after: str) -> list[DiffSpan]` — word-level, for the heal-log screen
  - `DiffSpan(kind: Literal["add","remove","same"], text: str)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/heallog/test_redaction_order.py
from pathlib import Path
import pytest
from selector_drift.heallog.evidence import store, RedactionFailed
from bok_core.boundary import Sensitivity

def test_redaction_failure_writes_nothing(leaky_evidence, tmp_path):
    """Un-redacted evidence is never written to be cleaned up later.
    There is no later."""
    with pytest.raises(RedactionFailed):
        store(leaky_evidence, Sensitivity.CLIENT_CONFIDENTIAL)
    assert not list(tmp_path.rglob("*"))

def test_stored_evidence_contains_no_original_identifier(evidence_with_pii):
    uri = store(evidence_with_pii, Sensitivity.CLIENT_CONFIDENTIAL)
    assert "Jane Okafor" not in Path(uri).read_text()

def test_synthetic_evidence_is_stored_and_is_publishable(bench_evidence):
    """Spec 8.2. All three day-one targets are SYNTHETIC, so the published heal
    log can carry real screenshots with no redaction argument."""
    assert store(bench_evidence, Sensitivity.SYNTHETIC)
```

```python
# tests/heallog/test_diff.py
from selector_drift.heallog.diff import dom_diff

def test_diff_is_word_level_not_line_level():
    spans = dom_diff("<td class=a>X</td>", "<td class=b>X</td>")
    assert [s.text for s in spans if s.kind == "remove"] == ["class=a"]
    assert [s.text for s in spans if s.kind == "add"] == ["class=b"]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/heallog -v`
Expected: FAIL.

- [ ] **Step 3: Implement `store()` and `dom_diff()`.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/heallog -v`
Expected: PASS, 4 new tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/heallog/evidence.py selector_drift/heallog/diff.py tests/heallog
git commit -m "feat(heallog): capture-redact-verify-store with word-level DOM diff"
```

---

# Phase G — API and UI (weeks 19–20)

---

### Task 34: The API

**Files:**
- Create: `selector_drift/api/main.py`, `targets.py`, `runs.py`, `heals.py`, `quarantine.py`, `quality.py`, `specs.py`
- Test: `tests/api/test_endpoints.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `GET /targets`, `GET /runs`, `GET /heals/{id}`, `POST /quarantine/{run_key}/promote`, `POST /quarantine/{run_key}/discard`, `GET /quality/{run_key}`, `GET /specs/{target}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_endpoints.py
import pytest

@pytest.mark.parametrize("path", ["/targets", "/runs", "/heals/1", "/quality/k", "/specs/testbench"])
def test_get_endpoints_return_200(client, path):
    assert client.get(path).status_code == 200

def test_promote_endpoint_requires_an_actor_and_a_reason(client):
    assert client.post("/quarantine/k/promote", json={}).status_code == 422

def test_targets_endpoint_surfaces_the_scope_record(client):
    t = client.get("/targets").json()[0]
    assert {"basis", "authorising_party", "expires_on", "rate_limit"} <= t.keys()

def test_heal_response_names_the_confirming_rung(client):
    assert "confirmer_rung" in client.get("/heals/1").json()

def test_discard_is_recorded_with_its_actor(client):
    client.post("/quarantine/k/discard", json={"actor": "me", "reason": "wrong column"})
    assert client.get("/runs").json()[0]["state"] == "quarantined"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/api -v`
Expected: FAIL.

- [ ] **Step 3: Implement the routers.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `uv run pytest tests/api -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/api tests/api
git commit -m "feat(api): FastAPI surface"
```

---

### Task 35: Web shell and the Targets screen

**Files:**
- Create: `web/app/layout.tsx`, `web/app/targets/page.tsx`, `web/components/ScopeCard.tsx`
- Test: `web/e2e/targets.spec.ts`

**Interfaces:**
- Consumes: `GET /targets` (Task 34), `bok-ui` tokens.
- Produces: light-first shell, drift indigo `oklch(0.55 0.14 268)` reserved for the heal state, tabular numerals on every figure.

- [ ] **Step 1: Write the failing E2E tests**

```typescript
// web/e2e/targets.spec.ts
test('a target without a scope record renders locked with the reason', async ({ page }) => {
  await page.goto('/targets');
  const locked = page.getByTestId('target-unscoped');
  await expect(locked).toHaveAttribute('aria-disabled', 'true');
  await expect(locked).toContainText('No authorisation record');
});

test('every scoped target surfaces its basis, party, expiry and rate limit', async ({ page }) => {
  await page.goto('/targets');
  const card = page.getByTestId('scope-card').first();
  for (const field of ['basis', 'authorising-party', 'expires-on', 'rate-limit']) {
    await expect(card.getByTestId(field)).not.toBeEmpty();
  }
});
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd web && npx playwright test e2e/targets.spec.ts`
Expected: FAIL.

- [ ] **Step 3: Build the shell and the screen.** Compliance posture is visible, not buried — it is a selling point.

- [ ] **Step 4: Run them and make sure they pass**

Run: `cd web && npx playwright test e2e/targets.spec.ts`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add web/app/layout.tsx web/app/targets web/components/ScopeCard.tsx web/e2e/targets.spec.ts
git commit -m "feat(web): shell and Targets screen"
```

---

### Task 36: Run timeline

**Files:**
- Create: `web/app/runs/page.tsx`, `web/components/RunTimeline.tsx`
- Test: `web/e2e/runs.spec.ts`

**Interfaces:**
- Consumes: `GET /runs` (Task 34).
- Produces: horizontal timeline — green completed, indigo healed, amber quarantined, red failed. **State is never colour alone**; each carries a glyph and a text label.

- [ ] **Step 1: Write the failing E2E tests**

```typescript
// web/e2e/runs.spec.ts
test('every run state exposes a text label, not only a colour', async ({ page }) => {
  await page.goto('/runs');
  for (const chip of await page.getByTestId('run-chip').all()) {
    await expect(chip).toHaveText(/completed|healed|quarantined|failed|refused/);
  }
});

test('clicking a run opens its full record', async ({ page }) => {
  await page.goto('/runs');
  await page.getByTestId('run-chip').first().click();
  await expect(page.getByTestId('run-detail')).toBeVisible();
});
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd web && npx playwright test e2e/runs.spec.ts`
Expected: FAIL.

- [ ] **Step 3: Build the timeline.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `cd web && npx playwright test e2e/runs.spec.ts`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add web/app/runs web/components/RunTimeline.tsx web/e2e/runs.spec.ts
git commit -m "feat(web): run timeline"
```

---

### Task 37: The heal-log screen — the signature

**Files:**
- Create: `web/app/heals/[id]/page.tsx`
- Create: `web/components/BeforeAfter.tsx`, `DomDiff.tsx`, `VerdictTable.tsx`, `ElementOutline.tsx`
- Test: `web/e2e/heallog.spec.ts`

**Interfaces:**
- Consumes: `GET /heals/{id}` (Task 34), `dom_diff` output (Task 33).
- Produces: two-column before/after with the resolved element outlined, word-level DOM diff beneath, the winning rung, **the cross-family confirmer**, every `Verdict` with its threshold, and the promote/discard decision with actor and timestamp.

- [ ] **Step 1: Write the failing E2E tests**

```typescript
// web/e2e/heallog.spec.ts
test('every verdict renders its threshold, never a bare badge', async ({ page }) => {
  await page.goto('/heals/1');
  for (const row of await page.getByTestId('verdict-row').all()) {
    await expect(row.getByTestId('threshold')).not.toBeEmpty();
    await expect(row.getByTestId('reason')).not.toBeEmpty();
  }
});

test('the outline animation is disabled under reduced motion', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/heals/1');
  await expect(page.getByTestId('outline')).toHaveCSS('animation-duration', '0s');
});

test('the confirming rung is named', async ({ page }) => {
  await page.goto('/heals/1');
  await expect(page.getByTestId('confirmer')).toContainText(/rung [234]/);
});

test('a non-reproducible heal says so', async ({ page }) => {
  await page.goto('/heals/non-reproducible');
  await expect(page.getByTestId('replay-status')).toContainText('not reproducible');
});
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd web && npx playwright test e2e/heallog.spec.ts`
Expected: FAIL.

- [ ] **Step 3: Build the screen.** The one orchestrated motion: the outline draws itself over 220 ms, first on "before" then on "after," so the eye tracks the move.

- [ ] **Step 4: Run them and make sure they pass**

Run: `cd web && npx playwright test e2e/heallog.spec.ts`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add web/app/heals web/components web/e2e/heallog.spec.ts
git commit -m "feat(web): heal-log screen with outlines, DOM diff and verdicts"
```

---

### Task 38: Quarantine, data quality, and the read-only spec view

**Files:**
- Create: `web/app/quarantine/page.tsx`, `web/app/quality/page.tsx`, `web/app/specs/[target]/page.tsx`
- Test: `web/e2e/quarantine.spec.ts`, `web/e2e/quality.spec.ts`

**Interfaces:**
- Consumes: the quarantine, quality and spec endpoints (Task 34).
- Produces: quarantine with data preview, stated reason and promote/discard; data quality as a **table with thresholds and reasons stated** (spec §10 — no plotted distributions in v1); spec view as **read-only YAML with schema validation** (no Monaco, no form view, no live preview).

- [ ] **Step 1: Write the failing E2E tests**

```typescript
// web/e2e/quarantine.spec.ts
test('empty quarantine states the passing count, and does not celebrate', async ({ page }) => {
  await page.goto('/quarantine');
  await expect(page.getByTestId('empty')).toContainText('Nothing quarantined');
  await expect(page.getByTestId('empty')).toContainText(/\d+ runs passed validation/);
});

test('promote requires a reason', async ({ page }) => {
  await page.goto('/quarantine');
  await page.getByRole('button', { name: 'Promote' }).click();
  await expect(page.getByRole('alert')).toContainText('reason');
});

test('a held run previews its data before any decision', async ({ page }) => {
  await page.goto('/quarantine');
  await expect(page.getByTestId('held-preview').first()).toBeVisible();
});
```

```typescript
// web/e2e/quality.spec.ts
test('every alert states its threshold and why that threshold', async ({ page }) => {
  await page.goto('/quality');
  for (const row of await page.getByTestId('quality-row').all()) {
    await expect(row.getByTestId('threshold')).not.toBeEmpty();
    await expect(row.getByTestId('threshold-reason')).not.toBeEmpty();
  }
});
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd web && npx playwright test e2e/quarantine.spec.ts e2e/quality.spec.ts`
Expected: FAIL.

- [ ] **Step 3: Build the three screens at the v1 scope above.**

- [ ] **Step 4: Run them and make sure they pass**

Run: `cd web && npx playwright test e2e/quarantine.spec.ts e2e/quality.spec.ts`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add web/app/quarantine web/app/quality web/app/specs web/e2e
git commit -m "feat(web): quarantine, data quality and read-only spec view"
```

---

### Task 39: Accessibility and full E2E

**Files:**
- Create: `web/e2e/a11y.spec.ts`, `web/e2e/keyboard.spec.ts`
- Create: `web/e2e/fixtures/` — E2E fixtures, kept separate from `selector_drift/browser/`'s own
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: every screen.
- Produces: axe zero serious/critical on all six routes; every flow reachable by keyboard.

- [ ] **Step 1: Write the failing tests**

```typescript
// web/e2e/a11y.spec.ts
import AxeBuilder from '@axe-core/playwright';

const ROUTES = ['/targets', '/runs', '/heals/1', '/quarantine', '/quality', '/specs/testbench'];

for (const route of ROUTES) {
  test(`${route} has no serious or critical axe violations`, async ({ page }) => {
    await page.goto(route);
    const r = await new AxeBuilder({ page }).analyze();
    expect(r.violations.filter(v => ['serious', 'critical'].includes(v.impact!))).toEqual([]);
  });
}
```

```typescript
// web/e2e/keyboard.spec.ts
test('quarantine promote is reachable and operable by keyboard alone', async ({ page }) => {
  await page.goto('/quarantine');
  await page.keyboard.press('Tab');
  while (!(await page.evaluate(() => document.activeElement?.textContent?.includes('Promote')))) {
    await page.keyboard.press('Tab');
  }
  await page.keyboard.press('Enter');
  await expect(page.getByRole('dialog')).toBeVisible();
});
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd web && npx playwright test e2e/a11y.spec.ts e2e/keyboard.spec.ts`
Expected: FAIL on at least one route.

- [ ] **Step 3: Fix every violation.**

- [ ] **Step 4: Run them, and wire axe into CI**

Run: `cd web && npx playwright test`
Expected: PASS, 7 tests. CI job `axe` added to `.github/workflows/ci.yml`.

- [ ] **Step 5: Commit**

```bash
git add web/e2e .github/workflows/ci.yml
git commit -m "test(web): axe and keyboard E2E in CI"
```

---

# Phase H — Documentation and the definition of done (week 20)

---

### Task 40: `methodology.md` and `security.md`

**Files:**
- Modify: `docs/methodology.md`, `docs/security.md`
- Create: `tests/docs/test_methodology_completeness.py`

**Interfaces:**
- Consumes: `rung_stats` (Task 6), the `runs` and `heal_events` tables.
- Produces: the document that makes every published number checkable.

`docs/methodology.md` must state: per-target start dates · which arm ran under which first-heal regime · what counts as a UI change · what counts as a successful heal · the sample and the population · the measured rung distribution including the rung-5 figure · why rung 5 is an escalation in v1 · the PSI threshold and why that threshold · the warm-up length · the control arm's deploy-history ground truth.

- [ ] **Step 1: Write the failing test**

```python
# tests/docs/test_methodology_completeness.py
from pathlib import Path

REQUIRED = ["start date", "what counts as a ui change", "what counts as a successful heal",
            "sample", "rung distribution", "rung 5", "psi threshold", "warm-up",
            "deploy history", "first-heal regime"]

def test_methodology_answers_every_required_question():
    text = Path("docs/methodology.md").read_text().lower()
    assert [q for q in REQUIRED if q not in text] == []

def test_security_documents_the_credential_posture():
    text = Path("docs/security.md").read_text()
    assert "TOTP" in text and "session injection" in text.lower() and "broker" in text.lower()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/docs -v`
Expected: FAIL with a list of missing sections.

- [ ] **Step 3: Write both documents,** taking every number from the live tables rather than from memory.

- [ ] **Step 4: Run it and make sure it passes**

Run: `uv run pytest tests/docs -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add docs/methodology.md docs/security.md tests/docs
git commit -m "docs: methodology and security posture"
```

---

### Task 41: Definition-of-done sweep

**Files:**
- Create: `tests/test_definition_of_done.py`

**Interfaces:**
- Consumes: everything.
- Produces: one executable test per DoD item, so "done" is a command rather than a claim.

- [ ] **Step 1: Write the ten tests**

```python
# tests/test_definition_of_done.py
import datetime as dt
from pathlib import Path
from selector_drift.resolve.cascade import REGISTERED_RUNGS
from selector_drift.orchestrate.rung_stats import rung_stats
from selector_drift.land.catalog import query

def test_dod_01_scope_gate_refuses_unauthorised_targets():
    assert run_state_for(unknown_target()) == "refused"

def test_dod_02_five_rungs_registered_with_instrumentation():
    assert [r.rung_id for r in REGISTERED_RUNGS] == [1, 2, 3, 4, 5]
    assert rung_stats()["rung5"] == 0.0   # escalation only in v1

def test_dod_03_every_adversarial_mutation_is_refused():
    assert all(is_quarantined(m) for m in ADVERSARIAL_MUTATIONS)

def test_dod_04_quarantine_promote_discard_audited():
    assert audit_rows_for("promote") and audit_rows_for("discard")

def test_dod_05_testbench_shipped_and_documented():
    assert Path("testbench/README.md").exists()
    assert all(m.name in Path("testbench/README.md").read_text() for m in ALL_MUTATIONS)

def test_dod_06_log_running_since_day_one():
    first = query("select min(started_at) from runs").fetchone()[0]
    assert (dt.date.today() - first.date()).days >= 1   # raise to 90 at day 90

def test_dod_07_landing_zone_idempotent_under_replay():
    before = query("select count(*) from lake").fetchone()[0]
    replay_last_run()
    assert query("select count(*) from lake").fetchone()[0] == before

def test_dod_08_no_totp_seed_and_posture_documented():
    assert "TOTP" in Path("docs/security.md").read_text()

def test_dod_09_heal_screen_renders_outlines_diff_and_verdicts():
    assert playwright_passed("web/e2e/heallog.spec.ts")

def test_dod_10_methodology_complete():
    assert pytest_passed("tests/docs/test_methodology_completeness.py")
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_definition_of_done.py -v`
Expected: some FAIL.

- [ ] **Step 3: Close every gap the failures name.**

- [ ] **Step 4: Run the full check plus the DoD sweep**

Run: *the full check*, then `uv run pytest tests/test_definition_of_done.py -v`
Expected: PASS, 10/10. Coverage ≥ 75%. `lint-imports` 5 kept, 0 broken. axe clean.

- [ ] **Step 5: Commit and tag**

```bash
git add tests/test_definition_of_done.py
git commit -m "test: executable definition of done"
git tag v1.0.0 && git push --tags
gh repo edit --visibility public --accept-visibility-change-consequences  # first release
```

---

## Sequencing notes

- **Tasks 1–6 are day one.** The ninety-day log's start date is set by Task 6's first cron fire and cannot be recovered later.
- **The log matures after the code does.** Day one of week 13 to the end of week 20 is 56 days. Publish the partial log at week 20 with its honest elapsed count, and update it at day 90 — Task 41's `test_dod_06` carries a `>= 1` assertion for week 20 and a comment to raise it to `>= 90` on the day. A partial log with an honest start date beats no log (B10).
- **Phase B before Phase C.** You cannot test a resolver against pages that do not change.
- **Task 17 after Tasks 14–16.** Rung 1 stores anchor evidence produced by whichever rung healed, so those rungs must exist first.
- **Task 22 is the task that justifies the project.** If only one thing in this plan is done carefully, it is cross-family confirmation.
