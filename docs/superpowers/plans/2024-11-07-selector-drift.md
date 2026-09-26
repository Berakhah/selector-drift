# Selector Drift Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A scheduled extraction pipeline that re-resolves page elements when a UI changes, proves each heal correct through an unskippable validation gate, and refuses to write when it cannot.

**Architecture:** A type-gated spine. Each stage returns a type the next stage structurally requires, so the two safety gates cannot be bypassed by forgetting to call them. `scope/` is the only module that can mint an `AuthorisedTarget`; `validate/` is the only module that can mint a `ValidatedExtraction`; `land.write()` accepts nothing else. A five-rung resolution cascade heals broken selectors, and a six-check gate judges every heal — including a cross-family re-resolution that catches the adjacent-column failure with no history needed.

**Tech Stack:** Python 3.12+, `uv`, `ruff`, `mypy --strict`, `import-linter`, Pydantic v2, Playwright (containerised, pinned), Prefect 3, Postgres + Alembic, Parquet + DuckDB, FastAPI, Next.js 15 + `bok-ui`, `bok-core` (PyPI), pytest + hypothesis, axe-core.

**Spec:** [`docs/superpowers/specs/2024-11-07-selector-drift-design.md`](../specs/2024-11-07-selector-drift-design.md)
**Audit:** [`docs/superpowers/reviews/2026-08-29-plan-audit.md`](../reviews/2026-08-29-plan-audit.md) — 63 findings; revisions R1–R17 are applied below and each carries its finding id.

## Week numbering

**Decided 2026-08-29 (audit OQ1): Selector Drift runs out of programme order, first.** Weeks 1–12 have
not run. Every week number below is **project-relative** — "week 13" is build week 1 (day one), "week
20" is build week 8, "weeks 17–18" are build weeks 5–6. **No calendar date is assumed anywhere.** Day
one's real date is whatever Task 6's first cron fire records in `docs/methodology.md`, and that
recorded date is the only start date the log ever claims (B4).

## Before day one — four preconditions, none of them code

Verified 2026-08-29 by `git log` across all five programme repos: **every repo contains docs only.**
No `bok_core`, no `template/`, no deployed demos. Tasks 1–6 cannot run until these are true.

| # | Precondition | Why it blocks | Audit |
|---|---|---|---|
| P-1 | `bok-core` **0.0.1** on PyPI containing `boundary/sensitivity.py` and `provenance/` (`Claim`, `Method`, `DomSource`) — nothing else | Task 1 `uv add bok-core`, Task 2 `Sensitivity`. `INTERFACE-CONTRACT.md` §8: a shim is *never* acceptable for `boundary`, so this cannot be worked around locally | B1 |
| P-2 | `copier` template published, and `USER` replaced with the real GitHub account everywhere | Task 1 Step 1; Task 5's `user_agent_contact` must be a URL that resolves — brief §3.3 requires the agent to identify itself with a *working* contact | B1, B5 |
| P-3 | The **three log targets named and reachable**. Design §8.1's control arm (`agent-perimeter`, `ledger-sense` demos) is **superseded** — OQ1 decided this project runs first, so those repos are ~12 weeks away. **Name two self-owned deployed applications that exist today**, and rename `scopes/*.yaml`, `specs/*.yaml` and Task 6's target list to match | Task 5, Task 6, design §8.1. The control arm's requirement is unchanged: targets we own, whose deploy history we can read, into which we do not author markup churn on purpose | B2 |
| P-4 | **Decided (OQ2): a free always-on VM** — Oracle Cloud Always Free (Ampere) or Fly.io. Provisioned, SSH-reachable, Docker installed, clock set to UTC, before day one. Still needs its **disk budget and retention policy** written down | 90 consecutive days of daily runs. Prefect persistence survives a *restart*, not a closed laptop. Also hosts the bench (OQ4) and sets the retention policy `00` B7 requires | B3, M30 |

`bok-core`'s remaining surface (`boundary.redact`, `boundary.egress.guard_write`, `findings`,
`testing.degraded`) is **not needed until Tasks 29/33 in weeks 17–18**. That is the slack: day one
needs two modules, not the whole substrate.

## Global Constraints

- Python 3.12+, `uv` for dependencies, `ruff` for lint+format, `mypy --strict` on every module. TypeScript strict mode, no `any` without a justifying comment.
- Licence **Apache-2.0**. Repo **private until first release (build week 8)**, per the programme decision — flipping a repo public preserves its full git history, so the ninety-day log's start date stays verifiable. The nightly cron runs on the P-4 VM, not on Actions, so the Actions allowance constrains CI only.
- **$0 recurring cost.** No dependency with a paid floor.
- **No secrets in the repo, ever, including fixtures.** `gitleaks` pre-commit + CI.
- **Zero model calls in v1.** No `bok_core.gateway` import anywhere. Rung 5 escalates; it does not call a model.
- **No `[project.scripts]`, no Typer/Click.** Dev entrypoint is `python -m selector_drift`. Buyer entrypoint is the Prefect scheduler.
- **No TOTP seed storage.** No field named or typed for a TOTP seed exists anywhere in the schema. Enforced by a test (Task 31).
- **No write-back into a client portal.** `land/` writes to our lake only; the change-set export (Task 30) produces a file the client applies.
- Scope bases in v1 are exactly `self-owned` and `client-authorised`. `public-permissive` is refused.
- Everything runs from `docker compose up` with only `.env.example` copied.
- Coverage floor 75%. axe zero serious/critical.
- Every subprocess containerised: non-root, read-only rootfs, tmpfs scratch, **seccomp default — never `unconfined`**, `cap_drop: [ALL]`, `no-new-privileges`, memory and pid limits, hard timeout. (R8/H1)
- **No `eval`, no `exec`, no `yaml.load`.** Every YAML loader is `yaml.safe_load`; every spec-supplied expression goes through the restricted evaluator from Task 21. A task spec is config, and config must never be code. (R13/H5)
- **The browser never leaves the authorised origin.** Every request is checked against an allowlist derived from the `AuthorisedTarget`; anything else is aborted. (R8/H2)
- **The API authenticates, and `actor` is never taken from a request body.** (R16/H3)
- **Every model of the world that a published number depends on is a table, not a log line.** `resolutions`, `run_attempts` and `deploys` ship on day one with the rest of the frozen schema. (R5/C4)

**Standing verification commands.** Split by tier so the gate after each task stays under a minute
and therefore actually gets run (M26). `pytest.ini` registers the markers; every test carries exactly
one.

```bash
# the fast check — after every task. No browser, no Postgres, no network.
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict selector_drift
uv run lint-imports
uv run pytest -q -m unit -n auto --cov=selector_drift --cov-fail-under=75

# the full check — at each phase boundary and before any commit that closes a task
uv run pytest -q -m "unit or integration" -n auto --cov=selector_drift --cov-fail-under=75
cd web && npx playwright test          # e2e, browser-backed
```

| Marker | May touch | Runtime budget |
|---|---|---|
| `unit` | nothing external. Rungs run against a `StaticPageSource` (Task 13) — **no browser** | < 60 s whole tier |
| `integration` | Postgres, the lake, a local bench fixture | < 5 min |
| `e2e` | a real browser and a served UI | unbounded, CI only |

---

# Phase A — Day one. The log starts. (Build week 1, day 1)

**Tasks 1–6 are one block and must all land on day one.** The ninety-day log takes calendar time that cannot be compressed. Do not begin Task 7 until the Prefect cron has fired at least once and written a run record. Day one logs *run succeeded / run failed* and nothing more; heal events appear around week 15 as the cascade lands, and the start date stays honest because the run record was always there.

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
uv add pydantic playwright prefect duckdb pyarrow fastapi sqlalchemy alembic psycopg pyyaml        lxml cryptography httpx numpy bok-core
uv add --dev pytest pytest-cov pytest-asyncio pytest-httpx pytest-xdist hypothesis              import-linter mypy ruff
uv run playwright install --with-deps chromium
```

**Changes from the first draft of this list (M12, P2).** `lxml` is required by name in Task 9 and was
never declared; `cryptography` by Task 32; `pytest-asyncio` by Task 4's `@pytest.mark.asyncio`;
`pytest-httpx` by Task 32's `httpx_mock`; `httpx` by the broker. **`scipy` is dropped** — it was there
for one statistic. PSI is `sum((a - e) * log(a / e))` over binned proportions, twenty lines of
`numpy`, and `scipy` is ~100 MB of wheels against a `docker compose up`-on-a-fresh-machine constraint.

`gitleaks` is a Go binary, not a Python package (L5). Install it in the image and invoke it through
`pre-commit run gitleaks --all-files`, not `uv run gitleaks`.

Pin the browser build in `pyproject.toml` under `[tool.selector-drift]` as `playwright_chromium_revision = "<revision printed by the install>"`. B8: an unpinned browser turns a UI-change test into a browser-change test.

- [ ] **Step 3: Write the import contracts**

Create `.importlinter`:

```ini
[importlinter]
root_packages = selector_drift, testbench
# REQUIRED (M21). Without it the bok_core.gateway contract names a module outside the graph,
# matches nothing, and reports "kept" — a green tick with no test behind it.
include_external_packages = True

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
# NB (C6): forbidden contracts report INDIRECT imports by default. Task 19's replay imports
# resolve.cascade, so this contract only holds because Task 13 moves PageSource out of browser/
# into resolve/page.py. Do not add allow_indirect_imports to make this pass — that would be
# suppressing the finding rather than fixing it.

[importlinter:contract:judge-reaches-rungs-only-by-protocol]
name = validate/ may reach rungs only via the public Rung protocol
type = forbidden
source_modules = selector_drift.validate
forbidden_modules = selector_drift.resolve.rungs
# Design 3.4 claims this contract; without this stanza it was never implemented (M22).
# Concrete rungs are constructed in orchestrate/ and injected; validate/ sees only Rung + Family.

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
mkdir -p selector_drift/{db,spec,scope,credentials,browser,resolve/rungs,validate/assertions,extract,quality,land,orchestrate,heallog,api}
mkdir -p testbench/{portal,mutations,mutations/adversarial,answers}
# db/ was missing and Task 3 writes into it (M24).
# testbench/portal is a Next.js app: it must NOT become a Python package, or grimp walks
# node_modules on every lint-imports run (L8).
for d in $(find selector_drift testbench -type d -not -path 'testbench/portal*'); do
  touch "$d/__init__.py"
done
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
- Create: `selector_drift/scope/record.py`, `selector_drift/scope/gate.py`, `selector_drift/scope/errors.py`, `selector_drift/scope/_token.py`
- Test: `tests/scope/test_gate.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ScopeRecord` (Pydantic v2) with fields `target: str`, `basis: Basis`, `authorising_party: str`, `authorised_on: date`, `expires_on: date`, `evidence_uri: str | None`, `evidence_sha256: str | None`, `origins: list[str]`, `rate_limit: RateLimit`, `user_agent_contact: str`, `first_heal_policy: FirstHealPolicy`, `legal_review: str`
  - **`sensitivity` is not an author-writable field (R4/H7).** It is a derived property:
    `SELF_OWNED -> SYNTHETIC`, `CLIENT_AUTHORISED -> CLIENT_CONFIDENTIAL`. A free-text label that
    gates publication means one typo in one YAML file publishes a client's portal screenshots — the
    exact outcome B4 exists to prevent, reachable with no second control. Derived, it cannot be typed
    wrong.
  - `origins: list[str]` — the only origins this target may reach. Consumed by Task 4's allowlist (H2)
  - `evidence_sha256` — **required whenever `evidence_uri` is set (R3/H10).** The scope record is the
    document you point at if anyone asks why you accessed a system; "trust the working tree" is not
    integrity. `authorise()` re-hashes the referenced file and refuses on mismatch
  - `Basis` — `StrEnum` with exactly `SELF_OWNED = "self-owned"` and `CLIENT_AUTHORISED = "client-authorised"`
  - `FirstHealPolicy` — `StrEnum` with `REQUIRED = "required"`, `AUTO_PROMOTE = "auto_promote"`
  - `RateLimit(requests: int, per_seconds: int)`
  - `AuthorisedTarget` — frozen dataclass; constructor requires the **singleton** `_MINT` token from
    `scope/_token.py`, and `__post_init__` asserts *identity*, not merely type (M29). A leading
    underscore is a convention, not a control: `from ... import _ScopeToken` works. The real
    enforcement is the identity check plus an import-linter contract on the token module — add to
    `.importlinter`:

    ```ini
    [importlinter:contract:only-the-gate-mints]
    name = Only scope.gate may reach the mint token
    type = forbidden
    source_modules =
        selector_drift.browser
        selector_drift.resolve
        selector_drift.validate
        selector_drift.extract
        selector_drift.land
        selector_drift.orchestrate
        selector_drift.heallog
        selector_drift.api
    forbidden_modules = selector_drift.scope._token
    ```
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
        evidence_uri=None, evidence_sha256=None,
        origins=["https://bench.example.invalid"],
        rate_limit={"requests": 1, "per_seconds": 30},
        user_agent_contact="https://example.invalid/contact",
        first_heal_policy="required", legal_review="self_drafted",
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

def test_a_forged_token_is_rejected_by_identity_not_by_type():
    """M29. A leading underscore is a convention. Someone WILL import _ScopeToken --
    probably a test fixture at 2am. Type-checking the token catches nothing; only
    identity against the singleton minted in gate.py does."""
    from selector_drift.scope._token import _ScopeToken
    with pytest.raises(ScopeRefusal, match="mint"):
        AuthorisedTarget(_ScopeToken(), target="anything", basis=Basis.SELF_OWNED,
                         rate_limit=RateLimit(1, 30), origins=[], user_agent_contact="",
                         first_heal_policy="required", sensitivity="SYNTHETIC",
                         expires_on=dt.date(2027, 1, 1))

def test_sensitivity_is_derived_from_basis_and_cannot_be_authored():
    """R4/H7. SYNTHETIC skips the redaction argument and permits publication.
    If it is a field, one typo publishes a client portal."""
    assert "sensitivity" not in ScopeRecord.model_fields
    assert rec(basis=Basis.SELF_OWNED).sensitivity == "SYNTHETIC"
    assert rec(basis=Basis.CLIENT_AUTHORISED, evidence_uri="file://a",
               evidence_sha256="0" * 64).sensitivity == "CLIENT_CONFIDENTIAL"

def test_client_authorised_evidence_must_hash_to_what_the_record_claims(tmp_path):
    """R3/H10."""
    doc = tmp_path / "authorisation.pdf"; doc.write_bytes(b"countersigned")
    with pytest.raises(ScopeRefusal, match="evidence_sha256"):
        authorise(rec(basis=Basis.CLIENT_AUTHORISED, evidence_uri=doc.as_uri(),
                      evidence_sha256="0" * 64), today=TODAY)

def test_a_record_with_no_origins_is_refused():
    """H2. An empty allowlist means the browser may go anywhere, which is the
    opposite of what a scope record is for."""
    with pytest.raises(ScopeRefusal, match="origins"):
        authorise(rec(origins=[]), today=TODAY)

def test_authorised_on_after_expires_on_is_refused():
    with pytest.raises(ScopeRefusal):
        authorise(rec(authorised_on=dt.date(2027, 1, 1),
                      expires_on=dt.date(2026, 12, 1)), today=TODAY)

def test_expiry_is_evaluated_in_utc_never_local_time():
    """M20. The run window, the mutation seed and this check must agree on a
    calendar or the bench's answer key drifts by a day at the boundary."""
    import selector_drift.scope.gate as g
    assert "date.today()" not in inspect.getsource(g)
```

**`today` is always passed in, never defaulted, and every caller passes
`datetime.now(UTC).date()`** (M20). Note also that the fixtures above use a fixed `TODAY` rather than
`dt.date.today()`: a test that compares a wall clock to a hardcoded `expires_on` is green until that
date and red afterwards for no reason anyone will remember.

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/scope -v`
Expected: FAIL — `ModuleNotFoundError: selector_drift.scope.record`.

- [ ] **Step 3: Implement the record and the token-gated mint site**

`_token.py` holds the mint control and nothing else, so the import-linter contract above has
something to point at:

```python
class _ScopeToken:
    __slots__ = ()

_MINT = _ScopeToken()      # the only instance the gate will accept
```

`record.py` defines the enums, `ScopeRecord`, the derived `sensitivity` property, and:

```python
@dataclass(frozen=True)
class AuthorisedTarget:
    _token: _ScopeToken
    target: str
    basis: Basis
    rate_limit: RateLimit
    origins: tuple[str, ...]          # H2 -- the browser allowlist
    user_agent_contact: str
    first_heal_policy: FirstHealPolicy
    sensitivity: Sensitivity          # derived from basis, never authored
    expires_on: date
    record_sha256: str                # H10 -- what this run will write to audit_log

    def __post_init__(self) -> None:
        if self._token is not _MINT:              # identity, not isinstance (M29)
            raise ScopeRefusal("not minted by the scope gate")
```

`gate.py` implements `authorise()`, refusing on each of, in order:

| Refusal | Audit |
|---|---|
| `expires_on < today` | brief §3.1 |
| `authorised_on > expires_on` | — |
| `basis` outside the v1 enum | design §4.2 |
| `basis is CLIENT_AUTHORISED and evidence_uri is None` | design §4.2 |
| `evidence_uri` set and its bytes do not hash to `evidence_sha256` | R3/H10 |
| `origins` empty | R8/H2 |
| `rate_limit.requests < 1 or per_seconds < 1` | H4 |

On success it returns `AuthorisedTarget(_MINT, ..., record_sha256=sha256(yaml_bytes))`. Every run
writes that hash to `audit_log`, so "which authorisation was in force when we accessed this system"
is answerable from the database rather than from the working tree.

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
| `runs` | `run_key` (PK, `f"{target}:{spec_version}:{window}"`), `target`, `spec_version`, `window`, `first_started_at`, `last_finished_at`, `state` (`completed`/`healed`/`quarantined`/`failed`/`refused`), `rows_written`, `error` |
| **`run_attempts`** | `id`, `run_key` FK, `attempt` (int), `started_at`, `finished_at`, `state`, `error`, `scope_record_sha256`. **New (R5/M13)** |
| **`resolutions`** | `id`, `run_key` FK, `target`, `field`, `winning_rung` (nullable — null means no rung resolved), `attempts` (JSONB, one entry per rung tried), `node_id`, `ms`, `at`. **New (R5/C4)** |
| `heal_events` | `id`, `run_key` FK, `resolution_id` FK, `target`, `field`, `broke_at`, `winning_rung`, `confirmer_rung`, `confirmer_family`, `selector_before`, `selector_after`, `anchor_before` (JSONB), `anchor_after` (JSONB), `trace` (JSONB), `verdicts` (JSONB), `evidence_uri`, `replayed_ok` (nullable bool), `promoted_by`, `promoted_at` |
| `selector_cache` | `target`, `field`, **`spec_version`** (composite PK), `selector`, `anchor` (JSONB), `node_id`, `set_by_rung`, `set_at`, `set_by_run_key` |
| `quarantine` | `run_key` FK, `reason`, `verdicts` (JSONB), `data_uri`, `resolved_as` (`promoted`/`discarded`/null), `actor`, `resolved_at` |
| `audit_log` | `id`, `at`, `actor`, `action`, `subject`, `detail` (JSONB) |
| `scope_records` | mirror of `ScopeRecord` fields plus `record_sha256`, `loaded_at` |
| **`deploys`** | `target`, `deployed_at`, `source` (`self-host`/`gh-actions`/`manual`), `ref`, `ingested_at`. **New (R17/M10)** — the control arm's ground truth |
| **`interventions`** | `id`, `at`, `actor`, `kind` (`spec_edit`/`cache_edit`/`manual_promote`), `target`, `field`, `before` (JSONB), `after` (JSONB), `reason`. **New (R17/M1)** |

**Four tables were added here rather than later, and the reason is the rule this task already
states.** *"None of them may add a column afterwards, because you cannot compare day 1 to day 90
across a schema migration."* That cuts both ways: a table missing on day one is missing for the whole
log.

- **`resolutions` (C4).** `rung_stats()` is promised in Task 6, required as a *published* claim in
  Task 26 Step 6, and asserted in Task 41 — and `heal_events` only records runs where a heal
  happened. Rung-1 wins were recorded nowhere, so `rung1 82%` was not computable from any table in
  the plan. B11's headline (*"0.0% of resolutions required a model call"*) needs a denominator, and
  this is it.
- **`run_attempts` (M13).** `run_key` is deterministic by design, so a retry after a failure
  overwrites the failed row and the published *"99.2% run success"* is measured on a table that
  silently discards failures. `runs` keeps the terminal state; `run_attempts` keeps the history.
- **`deploys` (M10).** Design §8.1's control-arm claim is *"heals fired only on runs following an
  actual deploy, **verified against deploy history**"*. Unverifiable without a stored feed.
- **`interventions` (M1).** *"Zero manual selector fixes"* is only a claim if manual fixes are
  recorded. Without this table the flagship number has exactly the epistemic status of the vendor
  numbers `00` was written to reject.

**Two key changes, same reasoning:**

- `selector_cache` PK gains `spec_version` (M14). Without it, editing a `FieldTarget.description` —
  i.e. changing what the field *means* — leaves the next run taking a rung-1 cache hit on a selector
  resolved for the old meaning, with the old anchor verifying happily.
- `heal_events.resolution_id` FK and `node_id` on both `resolutions` and `selector_cache`, so a heal
  is joinable to the resolution that produced it and node identity survives into the cache (C5).

**Indexes, also frozen now (M15/P3):** `runs(first_started_at)`, `run_attempts(run_key)`,
`resolutions(run_key)`, `resolutions(target, field, at)`, `heal_events(run_key)`,
`heal_events(target, broke_at)`, `deploys(target, deployed_at)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_schema.py
from sqlalchemy import inspect
from selector_drift.db.session import engine

EXPECTED = {"runs", "run_attempts", "resolutions", "heal_events", "selector_cache",
            "quarantine", "audit_log", "scope_records", "deploys", "interventions"}

def test_all_tables_exist():
    assert EXPECTED <= set(inspect(engine).get_table_names())

def test_heal_events_carries_anchor_columns_from_day_one():
    cols = {c["name"] for c in inspect(engine).get_columns("heal_events")}
    assert {"anchor_before", "anchor_after", "confirmer_rung", "confirmer_family",
            "replayed_ok", "resolution_id"} <= cols

def test_every_resolution_is_recorded_not_only_the_heals():
    """C4. rung_stats() is a published claim (B11) and heal_events only holds heals,
    so rung-1 wins would have no home and rung1 % would be uncomputable -- permanently,
    because this schema is frozen today."""
    cols = {c["name"] for c in inspect(engine).get_columns("resolutions")}
    assert {"run_key", "field", "winning_rung", "attempts", "node_id"} <= cols

def test_a_retry_does_not_erase_the_failed_attempt():
    """M13. run_key is deterministic, so without run_attempts a retry overwrites the
    failure and inflates the published run-success rate."""
    cols = {c["name"] for c in inspect(engine).get_columns("run_attempts")}
    assert {"run_key", "attempt", "state", "error"} <= cols

def test_the_cache_is_keyed_by_spec_version():
    """M14. Otherwise a spec edit silently reuses a selector resolved for the old meaning."""
    pk = {c.name for c in inspect(engine).get_pk_constraint("selector_cache")["constrained_columns"]}          if False else set(inspect(engine).get_pk_constraint("selector_cache")["constrained_columns"])
    assert pk == {"target", "field", "spec_version"}

def test_the_indexes_the_timeline_and_rung_stats_need_exist():
    names = {i["name"] for i in inspect(engine).get_indexes("resolutions")}
    assert names, "resolutions is scanned by rung_stats on every publish (M15)"
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
- Create: `selector_drift/browser/driver.py`, `selector_drift/browser/registry.py`, `selector_drift/browser/limits.py`, `selector_drift/browser/allowlist.py`, `selector_drift/browser/ratelimit.py`
- Modify: `docker-compose.yml` (add the `worker` service)
- Test: `tests/browser/test_driver.py`

**Interfaces:**
- Consumes: `AuthorisedTarget` (Task 2).
- Produces:
  - `open(target: AuthorisedTarget, credentials: Credentials | None = None) -> Session` — async context manager
  - `Session.page: PageHandle`, `PageHandle.url: str`, `PageHandle.content() -> str`, `PageHandle.screenshot() -> bytes`
  - `registry.url_for(target: str) -> str` — the only place a URL string lives
  - `allowlist.install(context, target) -> None` — **R8/H2.** A Playwright route handler that aborts
    every request whose origin is not in `target.origins`. The scope gate's guarantee is *"no path to
    a page without a scope record"*; that is true of the first navigation and of nothing after it. A
    redirect, an SSO hop, a link click in a nav step or an absolute URL in a spec walks straight off
    the authorised target while still inside an `AuthorisedTarget` context.
  - `ratelimit.acquire(target) -> None` — **R8/H4.** A **persistent** token bucket keyed by target,
    backed by the `audit_log`/Postgres rather than by process memory. The brief requires the limit in
    the scope record (§3.1), the brief's test list names *"rate limit enforced"* (§11), and design
    §4.3 says it is read from the `AuthorisedTarget` and *"is not a tunable"* — and until this
    revision it existed only as the phrase *"applies `target.rate_limit`"* with no step and no test.
    Per-run buckets are not enough: `1 req / 30 s` inside a run says nothing about ten runs starting
    at once.

- [ ] **Step 1: Write the failing tests**

```python
# tests/browser/test_driver.py
import inspect, pytest
from selector_drift import browser

def test_no_public_function_accepts_a_bare_url():
    """Weak by construction -- it only catches a parameter *named* `url` (M19). Kept as a
    typo guard; the real control is test_navigation_off_the_authorised_origin_is_aborted."""
    for name, fn in inspect.getmembers(browser.driver, inspect.isfunction):
        if name.startswith("_"):
            continue
        hints = inspect.get_annotations(fn, eval_str=True)
        assert str not in {hints.get(k) for k in ("url", "href", "path", "target_url")}, name

@pytest.mark.asyncio
async def test_open_loads_a_page(authorised_testbench):
    async with browser.driver.open(authorised_testbench) as session:
        assert "<html" in (await session.page.content()).lower()

@pytest.mark.asyncio
async def test_navigation_off_the_authorised_origin_is_aborted(authorised_testbench, evil_redirect):
    """R8/H2. This is the test the scope gate is *claimed* to have. Without it the gate
    authorises the first navigation and nothing after it."""
    async with browser.driver.open(authorised_testbench) as session:
        with pytest.raises(browser.OffScopeNavigation):
            await session.page.click("#leaves-the-portal")

@pytest.mark.asyncio
async def test_a_subresource_from_another_origin_is_aborted(authorised_testbench):
    async with browser.driver.open(authorised_testbench) as session:
        await session.page.goto_step({"step": "goto", "path": "/results"})
        assert all(r.startswith(tuple(authorised_testbench.origins))
                   for r in session.requested_urls)

def test_the_rate_limit_comes_from_the_target_and_not_from_config(authorised_testbench, monkeypatch):
    """H4, design 4.3: raising it means editing the scope record, which is the document
    you would point at if anyone ever asked why you accessed a system."""
    monkeypatch.setenv("SD_RATE_LIMIT_PER_SECONDS", "0")
    assert ratelimit.window_for(authorised_testbench) == 30

def test_the_limiter_is_persistent_across_processes(authorised_testbench):
    """Ten runs starting at once must not each get their own fresh bucket."""
    ratelimit.acquire(authorised_testbench)
    t0 = time.monotonic(); ratelimit.acquire(authorised_testbench, _fresh_process=True)
    assert time.monotonic() - t0 >= 29
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
    cap_drop: [ ALL ]
    security_opt:
      - no-new-privileges:true
      - seccomp=./docker/chromium-seccomp.json   # NOT unconfined -- see below
    tmpfs: [ "/tmp", "/home/app/.cache" ]
    shm_size: 1g              # Chromium's default /dev/shm in Docker is 64 MB
    mem_limit: 2g
    pids_limit: 512
    environment:
      PLAYWRIGHT_BROWSERS_PATH: /opt/pw-browsers   # baked at build, readable by uid 10001
    depends_on: [ postgres, prefect-server ]
```

**The first draft of this block read `security_opt: [ "seccomp=unconfined" ]` (H1).** `00` §8 and
design §9.2 both require *"seccomp default"*; `unconfined` **removes** the filter — the opposite of
the requirement — on the one container in the system that renders hostile HTML while holding session
credentials. It is there because Chromium's own sandbox needs privileges the default profile denies.
Pick one deliberately and write down which:

| Option | Trade |
|---|---|
| Chromium seccomp profile (above) | Keeps a filter. One vendored JSON file to review and pin |
| `--no-sandbox` under `cap_drop: [ALL]` + `no-new-privileges` | Simpler; leans entirely on the container boundary. Acceptable **only** because the container is single-purpose and network-restricted by the allowlist |

`shm_size`, `PLAYWRIGHT_BROWSERS_PATH` and the writable cache tmpfs are the three things that make
`read_only: true` plus a non-root uid actually boot (B8); the image installs browsers as root and the
service runs as `10001:10001`.

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
  - `TaskSpec` with `version: int`, `scope_ref: str`, `navigate: list[NavStep]`, `row_scope: str`, `natural_key: list[str]`, `expected_schema: dict[str, FieldType]`, `targets: list[FieldTarget]`, `schedule: str`
  - `FieldTarget(name: str, description: str, relation: Relation | None, assertions: list[AssertionSpec], distributional: bool = False)` — `description` is the semantic target in plain language, never a DOM path
  - `Relation` — `StrEnum` `RIGHT_OF`, `BELOW`, `WITHIN`
  - `matches(target: FieldTarget, value: str) -> bool` — **the format matcher lives here, in `spec/` (R13/M23).** Task 17's rung 1 must check format assertions and Task 1's contract forbids `resolve` → `validate`, so the matcher cannot live in `validate/assertions/format.py`. Both packages import it from `spec/`.
  - `load_spec(path: Path) -> TaskSpec`, `load_scope(path: Path) -> ScopeRecord` — **`yaml.safe_load` only** (R13/H5)

**Three fields added over the first draft, each because something downstream needs them and has no
other source:**

| Field | Needed by | Audit |
|---|---|---|
| `natural_key: list[str]` | Task 28's *"upsert on the natural key"*, Task 30's changeset diff, and `RowKeyContinuity` (Task 22). The phrase "natural key" appeared in the plan with no field anywhere defining one | M5 |
| `row_scope: str` | The cascade resolves per field; a results table is N rows × M fields. Nothing said how per-field resolutions align into rows. `row_scope` names the repeating container ("each row of the results table"), and fields resolve *within* it | M5 |
| `expected_schema` | Task 29 tests `schema_drift` against a baseline that did not exist. Brief §4 lists *"expected schema"* as part of a task spec | M4 |

`FieldTarget.distributional` opts a field into `DistributionalAssertion` (Task 23). It defaults to
`False` because PSI over a high-cardinality identifier column is noise, and *"which fields get a
distributional check at all"* was unspecified (M16).

`scopes/testbench.yaml` — note `first_heal_policy: auto_promote`, permitted **only because the bench is synthetic with a known answer key** (spec §5.8):

```yaml
target: testbench
basis: self-owned
authorising_party: <your name>
authorised_on: <day one, UTC>          # P-4: not a guess. The date the first cron fires.
expires_on: <authorised_on + 1 year>
evidence_uri: null
evidence_sha256: null
origins: [ https://<bench-host> ]      # H2: the browser may reach nothing else
rate_limit: { requests: 1, per_seconds: 30 }
user_agent_contact: https://github.com/<real-account>/selector-drift   # P-2: must resolve
first_heal_policy: auto_promote
legal_review: self_drafted
# no `sensitivity:` -- it is derived from `basis` and cannot be authored (R4/H7)
```

The other two are identical but `first_heal_policy: required`.

`specs/testbench.yaml`:

```yaml
version: 1
scope_ref: scopes/testbench.yaml
schedule: "12 4 * * *"                 # UTC. The mutation seed, the run window and the
                                       # expiry check must all agree on one calendar (M20)
row_scope: each row of the results table
natural_key: [ claim_number ]
expected_schema:
  claim_number: string
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
    """`today` is pinned to the record's own authorised_on, not to the wall clock (M20):
    a test that compares dt.date.today() to a hardcoded expires_on is green until that
    date and red afterwards for no reason anyone will remember."""
    names = {"testbench", "agent-perimeter", "ledger-sense"}   # PLACEHOLDER — P-3 replaces the two
                                                               # control-arm names before day one (OQ1)
    got = {authorise(r, today=r.authorised_on).target
           for r in map(load_scope, Path("scopes").glob("*.yaml"))}
    assert got == names

def test_every_scope_record_names_at_least_one_origin():
    assert all(load_scope(p).origins for p in Path("scopes").glob("*.yaml"))

def test_no_scope_record_authorises_a_placeholder_contact_url():
    """B5. Brief 3.3 requires the agent to identify itself with a contact URL; a 404 is a
    compliance claim that fails the first time anyone checks it."""
    assert not any("USER" in load_scope(p).user_agent_contact
                   for p in Path("scopes").glob("*.yaml"))

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

def test_every_spec_declares_a_natural_key_that_exists_in_its_schema():
    """M5. Task 28 upserts on it, Task 30 diffs on it, Task 22 joins on it."""
    for p in Path("specs").glob("*.yaml"):
        spec = load_spec(p)
        assert spec.natural_key
        assert set(spec.natural_key) <= set(spec.expected_schema)

def test_the_loader_refuses_a_yaml_tag_that_would_construct_an_object():
    """R13/H5. yaml.safe_load, always. A task spec is config; config must never be code."""
    with pytest.raises(yaml.YAMLError):
        load_spec_text("!!python/object/apply:os.system ['echo pwned']")
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
  - `rung_stats() -> dict[str, float]` — proportion of resolutions won by each rung, keys `rung1`…`rung5`, **computed from the `resolutions` table (R5/C4)**, not from `heal_events`. Empty until Phase C. Task 26 and Task 41 both read it; it is the source of a published claim (B11), so its denominator is every resolution attempted, including rung-1 cache hits.
  - `record_attempt(run_key, attempt, state, error, scope_record_sha256)` — one `run_attempts` row per try (M13). `runs` holds the terminal state; a retry must not erase the failure it retried.
  - `heartbeat()` — writes a row every scheduled fire, successful or not, so a **missing** run is distinguishable from a run that never fired. A ninety-day log with silent holes is worth less than a shorter one with none (B3).

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

- [ ] **Step 5: Verify the cron actually fires — on the host that will still be running in ninety days**

```bash
docker compose up -d
uv run prefect flow-run ls --limit 5
psql -c "select run_key, state, first_started_at from runs order by first_started_at desc limit 5"
```

Expected: at least one row per target. **The ninety-day log has now started. Record today's UTC date
in `docs/methodology.md` as the per-target start date for all three targets.**

**This step is not done on a laptop (B3/P-4).** Prefect's persistence solves *host restarts*, which is
what B8 asks for; it does not solve a host that is switched off. Ninety consecutive daily runs need a
machine that is on at 04:12 every night.

**Decided 2026-08-29 (OQ2): a free always-on VM — Oracle Cloud Always Free, or Fly.io.** It also hosts
the bench (OQ4), so the browser reaches the bench over the VM's own network and the H2 origin allowlist
names one host. Three consequences the plan otherwise misses:

- **Build the image on the VM, or push a matching architecture to GHCR.** The free Oracle shape is
  `arm64`; a locally built `amd64` Playwright image will not run on it. Pin the browser for the VM's
  architecture (B8) and prove it before day one, not on the first night.
- **Set the host clock to UTC** and confirm the schedule fires with nobody logged in. 04:12 in a
  timezone assumed to be UTC silently shifts the whole log.
- **Free tiers reclaim idle instances.** Oracle Always Free reclaims instances it judges idle, and one
  nightly run may not clear that bar (**VERIFY** the current policy on the day). If it applies, keep
  the instance above the threshold or publish the gaps.

Record in `docs/methodology.md`:

- the host, and who can restart it;
- the retention policy for evidence and the lake, with a disk budget (`00` B7, M30);
- **what happens to the log if the host is down for a day** — the honest answer is a gap, and the
  methodology says so rather than the chart hiding it.

- [ ] **Step 5b: Wire one alert channel before anything can quarantine**

A quarantined run at 04:12 that notifies nobody stalls the log silently, and the plan otherwise has no
notification path at all (M2) while DoD 7 requires *"data-quality contracts **and alerting**"*. One
webhook is enough for v1 — a `Finding` at `HIGH` or above, a quarantine, or a missed heartbeat posts
to it. Fully expanded in Task 29A; a stub here so nothing later has to invent one.

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
- Create: `testbench/mutator/app.py` — the mutation service (see below)
- Create: `testbench/portal/middleware.ts` — **proxies rendered HTML through the mutator; contains no mutation logic**
- Test: `tests/testbench/test_schedule.py`, `tests/testbench/test_served_markup_matches_the_engine.py`

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

- [ ] **Step 3: Implement the seeded schedule and put the ONE engine in the request path**

**The first draft of this step could not work (C7).** It said *"the middleware calls
`mutations_for(today, 'testbench')` and pipes the rendered HTML through each mutation's `apply()`"* —
but `mutations_for` and `apply()` are Python (Tasks 8–11 specify `lxml`), and a Next.js middleware is
TypeScript on the Edge runtime. It cannot call Python. As written the bench would either serve
unmutated pages, or serve a **second, untested TypeScript implementation** of the mutations — in which
case the transforms the tests exercise and the transforms the log observes are different code, the
answer key is not ground truth, and the treatment arm of the ninety-day log proves nothing.

There is exactly one rule here: **the served markup and the tested markup come from the same
`apply()`.**

```
GET /results
  -> Next.js renders the page (real portal, real framework)
  -> middleware.ts POSTs the rendered HTML to the mutator with the UTC date
  -> testbench/mutator/app.py: mutations_for(date, target) -> apply() chain -> mutated HTML
  -> middleware returns it
```

The mutator is ~40 lines of FastAPI over the existing `Mutation` protocol. It ships in the same
container as the portal (or as a second free service), and it is the only code that mutates markup.

The served page also carries `<meta name="sd-mutation-seed" content="...">` listing the applied
mutation names. The log records what it saw **without importing the answer key**, which keeps Task 1's
isolation contract intact and makes a day's DOM reproducible from the page itself.

- [ ] **Step 3b: Prove the served markup is the engine's output**

```python
# tests/testbench/test_served_markup_matches_the_engine.py
def test_the_deployed_bench_serves_exactly_what_the_engine_produces(bench_html_for_day):
    day = dt.date(2026, 9, 9)
    served = bench_html_for_day(day)                       # over HTTP, from the deployment
    local = apply_all(mutations_for(day, "testbench"), render_fixture())
    assert normalise(served) == normalise(local)
```

Without this test C7 can regress silently at any point in ninety days, and nothing downstream would
notice — the heal log would keep filling with heals scored against an answer key describing a
different page.

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

### Task 11A: The rest of the adversarial set — R12 (H9, M25)

**Files:**
- Create: `testbench/mutations/adversarial/hidden_decoy.py`, `duplicate_label.py`, `value_swap.py`, `locale_flip.py`, `virtualised_rows.py`, `image_header.py`
- Test: `tests/testbench/test_adversarial.py` (extend)

**Interfaces:**
- Consumes: `Mutation` (Task 8).
- Produces: six further adversarial classes, bringing the set to seven.

**Why this task exists.** The brief asks for *"a set of adversarial mutations designed to induce a
plausible-but-wrong heal"*, brief §11 says this set *"matters more"* than the heal set, and the
sequencing note says cross-family confirmation is the thing to get right. The plan shipped **one**
(the decoy column). One adversarial case does not test a gate; it tests one branch of one check.

Each class below attacks a *different* rung or check, and each has a test asserting the gate
**refuses**:

| Class | What it attacks | Which control must catch it |
|---|---|---|
| `HiddenDecoy` | Rung 2. A visually hidden / `aria-hidden` / off-screen element carrying `aria-label="Claim Number"`, resolving ahead of the real one, whose values satisfy the format assertion | **Nothing currently does (H9).** No rung requires visibility. Fix in Task 13: every rung's candidate filter requires a non-zero box, no `aria-hidden`, no `display:none` ancestor |
| `DuplicateLabel` | Rung 1's *"exactly one"* and rung 3's anchor | Rung 1 must fail on two matches; rung 3 must refuse an ambiguous anchor rather than take the first |
| `ValueSwap` | Everything structural **and** everything textual — two same-format columns exchange values, markup untouched | Only `RowKeyContinuity` (Task 22) sees it. This is the class that proves the third family earns its place |
| `LocaleFlip` | `1,234.56` → `1.234,56`. Passes `FormatAssertion` if the pattern is loose | `DomainAssertion` range check, and the currency-sanity rule brief §5 asks for |
| `VirtualisedRows` | 10 of 100 rows in the DOM | The row-count contract (Task 29), not the resolver |
| `ImageHeader` | Rung 4's header verification when the header is an `<img alt="Amount">` | Header verification must read accessible text, not `textContent` |

- [ ] **Step 1: Write the failing tests** — one per class, each asserting `isinstance(result, QuarantinedRun)` **and** naming the verdict that refused it. Asserting only that the run quarantined would pass for the wrong reason: with `history=None` the distributional check returns `INSUFFICIENT`, which on a heal is already a `FAIL`. Task 26's decoy test gets this right; copy its shape.

- [ ] **Step 2: Run them and watch them fail.** Run: `uv run pytest tests/testbench/test_adversarial.py -v`

- [ ] **Step 3: Implement the six transforms.**

- [ ] **Step 4: Run the fast check.** Expected: PASS. Every adversarial class refused, each by a named verdict.

- [ ] **Step 5: Commit**

```bash
git add testbench/mutations/adversarial tests/testbench/test_adversarial.py
git commit -m "feat(testbench): six further adversarial mutation classes"
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

- [ ] **Step 1: Deploy the bench to the P-4 VM** as a container built from `testbench/portal`, behind
  the same reverse proxy as the API, at a stable URL.

**Decided 2026-08-29 (OQ4/M28): not Vercel.** Hobby is non-commercial and the bench is the artifact a
paying buyer is handed. Self-hosting on the VM that has to exist anyway for P-4 costs nothing extra,
keeps the licence argument off the table, and removes a second free-tier quota (`00` B6) from the
nightly path. Mutation is a runtime transform driven by a date-derived seed, so the bench needs no
platform feature beyond serving HTTP.

- [ ] **Step 2: Verify the deployed markup mutates by date**

```bash
curl -s "$BENCH_URL/results" | sha256sum
curl -s -H "X-Testbench-Date: 2026-09-10" "$BENCH_URL/results" | sha256sum
```

Expected: two different hashes.

**The first draft of this step could not verify anything (M27).** It read
`TESTBENCH_DATE_OVERRIDE=2026-09-10 curl ...`, which sets an environment variable for the local
`curl` process; `curl` does not forward it to a remote server. It would print two identical hashes,
and the likely response to that is to assume mutation works. The override has to travel in the
request — a header the deployed app reads, gated to non-production or to a shared secret so a public
bench cannot have its schedule steered by a stranger.

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
- Create: `selector_drift/resolve/page.py` — **the `PageSource` protocol (R9)**
- Create: `selector_drift/resolve/rungs/base.py`, `selector_drift/resolve/trace.py`, `selector_drift/resolve/cascade.py`
- Test: `tests/resolve/test_cascade.py`, `tests/resolve/test_page_source_parity.py`

**Interfaces:**
- Consumes: `FieldTarget` (Task 5). **Not `browser/`** — see below.
- Produces:
  - `PageSource` protocol in `resolve/page.py` — `dom() -> str`, `ax() -> AxNode`, `query(selector) -> list[Node]`, `node_id(node) -> str`, `visible(node) -> bool`. Two implementations: `LivePageSource` in `browser/` (Playwright) and `StaticPageSource` in `resolve/` (parsed DOM + a computed role/accessible-name mapping)
  - `Family` — `StrEnum` with `TEXT_SEMANTIC`, `STRUCTURAL`, **`VALUE`**, `NONE`
  - `Rung` protocol — `rung_id: int`, `family: Family`, `resolve(page: PageSource, target: FieldTarget) -> ResolvedElement | None`
  - `AnchorEvidence(accessible_name: str | None, label_text: str | None, header_text: str | None)`
  - `ResolvedElement(selector: str, node_id: str, anchor: AnchorEvidence, rung_id: int)`

**`PageSource` replaces `PageHandle` in every rung signature, and it is not a refactor for tidiness —
three separate things in the plan were unsatisfiable without it (R9/C6, P1, H9).**

1. **Replay could not work.** `.importlinter` forbids `heallog` → `browser`; Task 19's replay runs the
   cascade over a stored DOM; the rungs took a `PageHandle` from `browser/`. import-linter reports
   **indirect** imports by default, so `heallog → resolve → browser` breaks the contract the moment
   Task 19 lands — and even suppressed, rung 2 cannot build an accessibility tree from a string
   without a browser. With `PageSource`, `replay()` constructs a `StaticPageSource` and the contract
   holds because it is true.
2. **It is design §6's best idea, made real:** *"the mutation regression suite and the replay path are
   the same code."* A mutation test becomes "build a `StaticPageSource` from this mutated fixture,
   assert this rung and this verdict set" — with no browser, which is what moves the whole `unit`
   tier under a minute (M26).
3. **Snapshot once, resolve many (P1).** As originally drafted every rung call is one or more CDP
   round-trips, so five fields × four rungs × N rows is hundreds of round-trips per run, doubled again
   by `IndependentRungAgreement` on every heal. `LivePageSource` captures the DOM and the AX tree
   **once** per page and every rung then runs in-process.

`visible(node)` on the protocol is what closes H9: a hidden or off-screen element carrying
`aria-label="Claim Number"` currently resolves ahead of the real one at rung 2, and every value it
yields can satisfy the format assertion. **Every rung filters candidates through `visible()` before
returning.**
  - `RungAttempt(rung_id: int, ok: bool, reason: str, ms: int)`
  - `CascadeTrace(attempts: list[RungAttempt], winner: int | None)` with property `.healed: bool` — **`True` when `winner != 1`**. Task 20's asymmetry reads exactly this property.
  - `resolve_cascade(page: PageSource, target, cache) -> tuple[ResolvedElement, CascadeTrace]` — writes one `resolutions` row per call, win or lose (R5/C4), which is what gives `rung_stats()` a denominator
  - `node_id` is minted by the `PageSource`, **never derived from the selector (R10/C5)**. Live: inject a `data-sd-node` attribute and read it back. Static: the element's structural path in the parsed tree. Task 22 compares `node_id`; comparing `selector` strings would be wrong in both directions — `#claim` and `td:nth-child(1)` routinely name the same node, and the same selector string can be produced against different snapshots
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

def test_every_resolution_is_recorded_win_or_lose(fake_page, session):
    """C4. rung_stats() is a published claim and needs a denominator, not just heals."""
    resolve_cascade(fake_page, TARGET, EmptyCache())
    assert session.query(Resolution).count() == 1

def test_a_hidden_element_is_never_a_candidate(bench_page):
    """H9. A hidden node carrying the right accessible name resolves ahead of the real
    one and every value it yields satisfies the format assertion."""
    page = bench_page.mutated(HiddenDecoy())
    el, _ = resolve_cascade(page, CLAIM_NUMBER, EmptyCache())
    assert page.visible(page.node(el.node_id))

def test_node_id_is_not_derived_from_the_selector(bench_page):
    """C5. Two selectors naming the same node must yield the same node_id."""
    a = bench_page.resolve_by("#claim-number"); b = bench_page.resolve_by("td:nth-child(1)")
    assert a.node_id == b.node_id and a.selector != b.selector
```

```python
# tests/resolve/test_page_source_parity.py
@pytest.mark.parametrize("rung", REGISTERED_RUNGS)
def test_live_and_static_sources_resolve_identically(rung, bench_html):
    """R9. If the two implementations disagree, replay is theatre and every mutation
    test is testing something the log never saw."""
    live = LivePageSource.from_html(bench_html)
    static = StaticPageSource(bench_html)
    assert rung.resolve(live, CLAIM_NUMBER).node_id == rung.resolve(static, CLAIM_NUMBER).node_id
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
  - `SelectorCache.put(validated: ValidatedExtraction, element: ResolvedElement) -> None` — **the entire write-back. Self-healing is this line and nothing else** — and its *first argument is the gate's output type* (R11/C2).

**When `put()` is called is the whole safety argument, and the first draft did not say (C2).**
It described `put()` as the write-back and left the timing to the implementer. The obvious reading —
write the cache the moment a rung heals — is catastrophic and silent:

> heal at 04:12 → `put()` → gate quarantines the run → **the cache now holds the wrong selector with
> the wrong anchor** → 04:12 tomorrow, rung 1 matches exactly one element, the stored anchor verifies
> (it was stored from the bad heal), `trace.healed` is `False`, the strict path in Task 20 never
> engages, and the wrong column flows into the client's system every night from then on.

That is the adjacent-column failure made permanent, arriving through the one door §5.2 worked hardest
to close. Taking `ValidatedExtraction` — which only `validate/gate.py` can mint — makes the ordering
structural rather than a comment: **there is no way to spell "write the cache" that does not first
spell "the gate passed".**
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

def test_put_stores_the_anchor_from_the_healing_rung(warm_cache, healed_element, validated):
    warm_cache.put(validated, healed_element)
    assert warm_cache.get("testbench", "claim_number", spec_version=1).anchor == healed_element.anchor

def test_a_quarantined_heal_does_not_update_the_cache(warm_cache, quarantined_heal):
    """R11/C2. The single most dangerous ordering bug available in this design: a rejected
    heal that poisons the cache becomes a rung-1 hit tomorrow, records no heal, and never
    engages the strict path again."""
    before = warm_cache.get("testbench", "claim_number", spec_version=1)
    run_pipeline(quarantined_heal)
    assert warm_cache.get("testbench", "claim_number", spec_version=1) == before

def test_put_cannot_be_called_with_anything_but_a_validated_extraction(warm_cache, candidate):
    with pytest.raises(TypeError):
        warm_cache.put(candidate, ELEMENT)  # type: ignore[arg-type]

def test_a_cache_entry_for_another_spec_version_is_not_a_hit(bench_page, warm_cache):
    """M14. A spec edit changes what the field MEANS."""
    assert CacheRung(warm_cache, spec_version=2).resolve(bench_page, CLAIM_NUMBER) is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/resolve/test_r1_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: selector_drift.resolve.cache`.

- [ ] **Step 3: Implement the cache and the rung**

`CacheRung.resolve()` returns `None` unless **all four** hold: the selector matches exactly one
element; that element is `visible()` (H9); the stored `AnchorEvidence` still holds against it
(whichever of accessible name / label text / header text was recorded); and the element satisfies the
target's format assertions via **`spec.matches()`, not `validate.FormatAssertion`** — Task 1's
`judged-cannot-call-judge` contract forbids `resolve` → `validate`, so the matcher lives in `spec/`
and both packages import it from there (M23). Register in `REGISTERED_RUNGS` at position 0.

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
  - `replay(evidence: Evidence, target: FieldTarget, cache) -> tuple[ResolvedElement, CascadeTrace]` — builds a **`StaticPageSource` from the stored DOM** (Task 13) and runs the cascade against it, never a live page
  - `NotReproducible(Exception)`

`heallog/` cannot import `browser/` (Task 1 contract), so replay-from-stored-evidence is structural,
not a convention — **but only because Task 13 moved `PageSource` out of `browser/` (R9/C6).** With the
rungs still taking a `browser.PageHandle`, `heallog → resolve → browser` is an indirect import that
import-linter reports by default and the contract fails here. If it does fail, the fix is Task 13, not
`allow_indirect_imports`.

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
- Produces: `FormatAssertion(pattern: str)`, `DomainAssertion(rule: DomainRule)`, `CrossFieldAssertion(expression: str)`, `safe_eval(expression, row)`. All three assertions work on run one with no history.

**`CrossFieldAssertion` must not use `eval` (R13/H5).** The first draft gave it `expression: str` —
`"net + tax == gross"` — and did not say how it is evaluated. The default reading is `eval`, and today
that is only *"our own config"*: design §10 explicitly roadmaps screen 2 to *"an editor for
non-engineers ... which is also the trigger to build it"*, at which point a YAML file becomes remote
code execution inside the container that holds client session cookies. `safe_eval` walks the parsed
AST and permits only `Name` (bound to row fields), numeric literals, arithmetic, comparison and
boolean operators — no attribute access, no calls, no subscripts, no imports. Twenty lines, or
`asteval`/`simpleeval` if a dependency is cheaper than a review.

`FormatAssertion` also **caps input length before matching**: a pattern from a spec applied to a value
from a page is untrusted-input-meets-untrusted-pattern, and catastrophic backtracking hangs a run.

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

def test_a_spec_expression_cannot_reach_the_interpreter():
    """R13/H5. A task spec is config. Config must never be code -- least of all in the
    container holding client session cookies."""
    for hostile in ["__import__('os').system('id')",
                    "().__class__.__bases__[0].__subclasses__()",
                    "open('/etc/passwd').read()"]:
        with pytest.raises(UnsafeExpression):
            CrossFieldAssertion(hostile).check(rows([{}]), None, CTX)

def test_format_assertion_bounds_its_input():
    """A crafted page value against a backtracking pattern hangs the run."""
    v = FormatAssertion(r"^(a+)+$").check(rows(["a" * 10_000 + "b"]), None, CTX)
    assert v.outcome is Outcome.FAIL and "length" in v.reason.lower()
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
- Produces: `IndependentRungAgreement(rungs: list[Rung])`, `RowKeyContinuity(threshold: float = 0.95)`. Re-resolves the element with a confirmer from a **different `Family`** than the one that healed, and requires the two to point at the **same `node_id`** (R10/C5) — never the same selector string, which is wrong in both directions.

| Healed by | Valid confirmer |
|---|---|
| 2 or 3 (`TEXT_SEMANTIC`) | 4 (`STRUCTURAL`), or `VALUE` |
| 4 (`STRUCTURAL`) | 2 or 3 (`TEXT_SEMANTIC`), or `VALUE` |
| 1 | n/a — never a valid confirmer; it is what broke |

### R14/C3 — the third family, and why the gate needs one

**As originally specified, a plain label rewording quarantines every time, forever.** Trace the
design's own tables:

| Step | Result |
|---|---|
| Rung 2 (TEXT_SEMANTIC) | accessible name gone, returns `None` (Task 14 asserts this) |
| Rung 3 (TEXT_SEMANTIC) | label text gone, returns `None` (Task 15 asserts this) |
| Rung 4 (STRUCTURAL) | heals via `data-testid` (Task 16 asserts this) |
| `IndependentRungAgreement` | healed STRUCTURAL, so the confirmer must be TEXT_SEMANTIC, and both just failed, so `INSUFFICIENT` |
| §5.7 asymmetry | `INSUFFICIENT` on a heal becomes `FAIL`, so **quarantine** |

Label rewording is the most common real UI change there is. On the bench, mutations re-seed daily, so
it recurs with fresh strings — and `first_heal_policy: auto_promote` does **not** rescue it, because
that policy satisfies only `FirstHealConfirmation` while `IndependentRungAgreement` still fails.
Design §14's mitigation for *"log stalls on quarantine backlog"* therefore does not work, and the
ninety-day log backs up in week 14.

**The fix is in the brief already.** Brief §5 lists as a check: *"Cross-field consistency — does the
row still foot? **Do fields co-vary as they did historically?**"* Task 21 implemented only the footing
half. The historical half is a third, genuinely independent signal:

```python
class RowKeyContinuity:               # family = Family.VALUE
    # Join this run's rows to the previous accepted run's on spec.natural_key.
    # For keys present in both, require the healed field's values to agree at or
    # above `threshold`. Deterministic, no model, no distribution, no DOM.
```

| Mutation | Text family | Structural family | `VALUE` family | Outcome |
|---|---|---|---|---|
| `LabelReword` | fails | heals | **values unchanged, confirms** | accepted |
| `ColumnReorder` | heals | fails | values unchanged, confirms | accepted |
| `DecoyColumn` | points at Amount | points at Prior Amount | **values differ from last run, refuses** | quarantined |
| `ValueSwap` (Task 11A) | agrees | agrees | **refuses — the only check that sees it** | quarantined |

It is a *stronger* discriminator for the adjacent-column case than DOM agreement, because it looks at
the data rather than at the page — and `ValueSwap` is a mutation nothing else in the plan catches.

**This amends design §5.6, the design's most carefully argued section. Signed off 2026-08-29 (OQ3) —
implement it.** The conservative alternative — keep two families and say in the methodology that *"a
label change costs one human confirmation, once, and never again"* — was considered and rejected: the
bench re-seeds its mutations daily, so a reword returns with a fresh string and the confirmation is not
once, it is nightly. `VALUE` is also the only family that catches `ValueSwap` (Task 11A).

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

def test_agreement_compares_node_identity_not_selector_text():
    # R10/C5. `#claim` and `td:nth-child(1)` name the same node; two runs against
    # different snapshots can produce the same string for different nodes.
    v = IndependentRungAgreement([AxTreeRung(), HeuristicRung()]).check(
        candidate_where(selectors_differ=True, node_ids_match=True), None, ctx(3))
    assert v.outcome is Outcome.PASS

def test_a_label_reword_is_confirmed_by_value_continuity_and_is_not_quarantined(history):
    # R14/C3. Both text rungs fail on a reword, so without a VALUE confirmer this
    # quarantines forever -- and it is the most common real UI change there is.
    v = IndependentRungAgreement([AxTreeRung(), HeuristicRung(), RowKeyContinuity()]).check(
        reworded_candidate(), history, ctx(4))
    assert v.outcome is Outcome.PASS and v.observed == "row-key continuity"

def test_the_decoy_column_is_refused_by_value_continuity_too(history):
    v = RowKeyContinuity().check(DECOY_CANDIDATE, history, ctx(4))
    assert v.outcome is Outcome.FAIL and float(v.observed) < 0.95

def test_value_swap_is_caught_by_nothing_else(history):
    # Task 11A's ValueSwap leaves markup untouched, so every DOM signal agrees.
    dom = IndependentRungAgreement([AxTreeRung(), HeuristicRung()]).check(SWAPPED, history, ctx(4))
    assert dom.outcome is Outcome.PASS
    assert RowKeyContinuity().check(SWAPPED, history, ctx(4)).outcome is Outcome.FAIL

def test_continuity_with_no_history_is_insufficient_never_pass():
    assert RowKeyContinuity().check(CANDIDATE, None, ctx(4)).outcome is Outcome.INSUFFICIENT
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
- Produces: `DistributionalAssertion(threshold: float = 0.20, warmup_runs: int = 30, persistence: int = 2, bins: int = 10, epsilon: float = 1e-6)`.

**Two corrections to the first draft, and a specification it was missing.**

**(a) Persistence must not apply to the blocking path (M17).** As drafted, `FAIL` required PSI over
threshold on `persistence` consecutive runs — so a heal whose distribution shifted immediately got
`PASS` on run one, which is *the only run that matters*. Design §5.7's argument is *"a heal into a
shifted distribution blocks"*, and the plan's own
`test_seasonality_does_not_alert_before_persistence_is_met` asserted the opposite. Split it:

| Path | Rule | Why |
|---|---|---|
| Not healed (steady state) | persistence `2` before alerting; never blocks | B7 — month-end, seasonality and real business change must not produce an alert the client mutes |
| **Healed** | **a single-run breach is `FAIL`** | The heal is the event. Waiting for a second run means the first day's bad data is already written |

**(b) Warm-up must be per-target, from the scope record, not a global 30 (M18).** On a daily cron
`warmup_runs=30` is thirty days during which `DistributionalAssertion` returns `INSUFFICIENT`, which
on a heal is `FAIL`, which quarantines **every heal for the first month of the ninety-day log**. That
is correct in principle — there is no basis to trust it yet — but combined with C3 it means the
treatment arm produces nothing usable until week 17. The bench, being synthetic with a known answer
key, warms up in `warmup_runs: 5`; client targets keep 30. Like `first_heal_policy`, it lives on the
scope record so it cannot be quietly lowered for a client.

**(c) The statistic must be reproducible (M16).** The heal-log line the design is proud of —
*"distribution unchanged (PSI 0.03, threshold 0.20, n=847)"* — is not checkable by a reader unless all
of this is written down and published in `docs/methodology.md`:

| Choice | Value | Why this one |
|---|---|---|
| Binning | 10 quantile bins **fixed from the reference window**, not recomputed per run | Recomputing bins per run makes PSI compare a distribution to itself |
| Reference window | trailing 30 accepted runs for the same target and field | Named, so "n=847" has a defined denominator |
| Zero-bin handling | add `epsilon` to both proportions | PSI is undefined at `log(0)`; without this it silently returns `inf` |
| Categorical fields | category proportions, no binning | |
| Which fields at all | `FieldTarget.distributional` (Task 5), default `False` | PSI over a high-cardinality identifier is noise |

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
    # B7. One shifted run is month-end. Two in a row is a problem.
    # NOTE the context: NOT healed. Persistence governs alerting only.
    v = DistributionalAssertion().check(shifted(), history(n=40, consecutive_shifts=1), CTX_STEADY)
    assert v.outcome is Outcome.PASS

def test_a_single_run_shift_blocks_when_the_selector_healed():
    # M17. The heal IS the event. Waiting for a second consecutive run means the
    # first day's bad data is already written -- which is what the gate exists to stop.
    v = DistributionalAssertion().check(shifted(), history(n=40, consecutive_shifts=1), CTX_HEALED)
    assert v.outcome is Outcome.FAIL

def test_warmup_length_comes_from_the_scope_record_not_a_global():
    # M18. 30 daily runs is 30 days of quarantining every heal on the bench,
    # which stalls the treatment arm until week 17.
    assert DistributionalAssertion.for_target(BENCH).warmup_runs == 5
    assert DistributionalAssertion.for_target(CLIENT).warmup_runs == 30

def test_a_bin_with_no_observations_does_not_return_infinity():
    # M16. PSI is undefined at log(0). Without epsilon this silently returns inf,
    # which compares greater than every threshold and quarantines everything.
    v = DistributionalAssertion().check(with_empty_bin(), history(n=40), CTX_HEALED)
    assert float(v.observed) != float("inf")

def test_bins_are_fixed_from_the_reference_window_not_recomputed_per_run():
    # Recomputing quantile bins per run compares a distribution to itself and
    # PSI is ~0 no matter what moved.
    assert DistributionalAssertion().bin_edges_for(history(n=40)) ==            DistributionalAssertion().bin_edges_for(history(n=40, shifted_tail=True))

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
    # M31. Patch the name in the module that CALLS it. quarantine.py does
    # `from .gate import gate`, which binds at import, so patching
    # selector_drift.validate.gate.gate is invisible here and this test would pass
    # whether or not promotion re-runs the gate -- on the check that stops a human
    # waving data through.
    calls = []
    real = quarantine_mod.gate
    monkeypatch.setattr(quarantine_mod, "gate",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    promote(quarantined.run_key, actor=ACTOR, reason="checked the screenshots")
    assert calls == [1]

def test_promote_rejects_a_caller_supplied_actor(quarantined):
    # H3. An audit log whose actor is supplied by the party being audited is not
    # an audit log. The actor comes from the authenticated session (Task 34).
    with pytest.raises(TypeError):
        promote(quarantined.run_key, actor="someone-else", reason="x", _from_body=True)

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

### Task 26A: Assemble the pipeline — R7 (C1)

**Files:**
- **Modify: `selector_drift/orchestrate/flows.py`** — the only task in the plan that does
- Create: `selector_drift/orchestrate/pipeline.py`
- Test: `tests/orchestrate/test_pipeline_end_to_end.py`

**Interfaces:**
- Consumes: `authorise`, `browser.driver.open`, `resolve_cascade`, `extract.read`, `validate.gate`,
  `SelectorCache.put`, `land.write`, `heallog.capture`/`store`, `quality.evaluate`.
- Produces: an `extraction_flow` that actually runs the product.

**Why this task exists.** `flows.py` is created in Task 6, where `extraction_flow` deliberately
*"does nothing else — no cascade exists yet"*, and **it appears in no later task's file list.** Tasks
13–33 build the cascade, the gate, extraction, the lake, quality and evidence, and nothing wires them
together. Task 41's definition-of-done sweep then asserts `rung_stats()` and
`select count(*) from lake` against a pipeline that has never run end to end. Forty-one tasks
produced a parts bin.

This sits between Task 25 (the gate exists) and Task 26 (the suites measure it) because that is the
first moment it can be written and the last moment it can be skipped.

**The order is the safety argument. Write it once, here:**

```
authorise(scope)                       -> AuthorisedTarget      | ScopeRefusal -> state=refused
  open(target, credentials)            -> Session               | -> state=failed
  capture(page)                        -> Evidence              (before any resolution)
  for each field in spec.targets:
      resolve_cascade(page, field)     -> ResolvedElement, CascadeTrace
                                          + one `resolutions` row, win or lose      (C4)
  extract.read(elements, spec)         -> CandidateExtraction
  quality.evaluate(candidate)          -> list[Finding]         -> alert channel    (M2)
  validate.gate(candidate, history, ctx)
      |
      +-- ValidatedExtraction:
      |       land.write(validated)                                                  (idempotent)
      |       cache.put(validated, element)   <- ONLY here. Never at heal time.      (C2)
      |       heallog.store(evidence)  if trace.healed
      |       state = healed if trace.healed else completed
      |
      +-- QuarantinedRun:
              heallog.store(evidence);  quarantine row;  alert
              NO land.write.  NO cache.put.
              state = quarantined
```

**Two orderings in that diagram are load-bearing and neither was written down before:**

1. **`cache.put()` happens only on the `ValidatedExtraction` branch (R11/C2).** Writing the cache when
   a rung heals — the obvious reading of Task 17 — means a quarantined heal poisons the cache, the
   next run takes a rung-1 hit on the wrong selector, `trace.healed` is `False`, and the strict path
   never engages again. Task 17's `put(validated, element)` signature makes this impossible to spell
   wrongly; this diagram says why.
2. **`capture()` happens before resolution, not after.** Evidence must be of the page the resolver
   actually saw. Capturing after a heal captures a page that a `page.route` abort or a timeout may
   already have changed, and a heal you cannot reproduce is an anecdote (B5).

- [ ] **Step 1: Write the failing tests**

```python
# tests/orchestrate/test_pipeline_end_to_end.py
def test_a_clean_run_writes_rows_and_updates_nothing_else(bench, session):
    key = extraction_flow("specs/testbench.yaml")
    assert session.get(Run, key).state == "completed"
    assert rows_in_lake(key) > 0
    assert session.query(HealEvent).filter_by(run_key=key).count() == 0

def test_a_healed_run_writes_rows_a_heal_event_and_the_cache(bench_mutated, session):
    key = extraction_flow("specs/testbench.yaml")
    assert session.get(Run, key).state == "healed"
    assert session.query(HealEvent).filter_by(run_key=key).one().confirmer_rung
    assert cache_entry("testbench", "claim_number").set_by_run_key == key

def test_a_quarantined_run_writes_no_rows_and_does_not_touch_the_cache(bench_decoy, session):
    before = cache_entry("testbench", "amount")
    key = extraction_flow("specs/testbench.yaml")
    assert session.get(Run, key).state == "quarantined"
    assert rows_in_lake(key) == 0
    assert cache_entry("testbench", "amount") == before          # C2

def test_a_refused_target_never_opens_a_browser_and_never_captures(expired_spec, monkeypatch):
    opened = []
    monkeypatch.setattr("selector_drift.browser.driver.open", lambda *a, **k: opened.append(1))
    assert state_of(extraction_flow(expired_spec)) == "refused"
    assert opened == []

def test_every_resolution_lands_in_the_resolutions_table(bench, session):
    key = extraction_flow("specs/testbench.yaml")
    assert session.query(Resolution).filter_by(run_key=key).count() == len(SPEC.targets)

def test_a_failed_run_writes_an_attempt_row_and_a_retry_writes_a_second(flaky_bench, session):
    key = extraction_flow("specs/testbench.yaml")            # fails
    extraction_flow("specs/testbench.yaml")                  # retries, succeeds
    assert session.query(RunAttempt).filter_by(run_key=key).count() == 2   # M13
    assert {a.state for a in attempts(key)} == {"failed", "completed"}
```

- [ ] **Step 2: Run them and watch them fail.** Every one, for a different reason — nothing is wired.

- [ ] **Step 3: Implement `pipeline.py` and rewrite `extraction_flow` to call it.** The flow owns
  Prefect concerns (retries, timeout, logging); `pipeline.py` owns the order above and is a plain
  function, so it is testable without a scheduler.

- [ ] **Step 4: Run the full check.** Expected: PASS, 6 tests, and the run states in `psql` now
  include `healed` and `quarantined` for the first time.

- [ ] **Step 5: Commit**

```bash
git add selector_drift/orchestrate tests/orchestrate
git commit -m "feat(orchestrate): assemble the pipeline -- resolve, gate, land, log"
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

### Task 26B: The third suite — every benign heal is *accepted* — R14 (C3, M3)

**Files:**
- Create: `tests/suite/test_benign_heals_are_accepted.py`
- Create: `tests/suite/test_degraded_mode.py`

**Interfaces:**
- Consumes: the pipeline (Task 26A), all seven checks, all fifteen mutation classes.
- Produces: the false-positive half of the gate's measurement, and the degraded-mode floor.

**Why this task exists.** Task 26 asserts two things: benign mutations **resolve**
(`trace.winner in (2,3,4)`) and adversarial mutations are **refused**. Neither asserts that a benign
mutation is *accepted by the gate*. **A resolver that quarantines 100% of legitimate heals passes
every test in this plan** — and then the headline *"zero manual selector fixes"* is only true because
a human cleared the queue by hand every night, which is the opposite of the product.

Worse, per C3 the design **does** quarantine every label rewording until R14 lands. That defect was
invisible precisely because this suite did not exist.

- [ ] **Step 1: Write the failing acceptance suite**

```python
# tests/suite/test_benign_heals_are_accepted.py
@pytest.mark.parametrize("mutation", ALL_BENIGN_MUTATION_CLASSES, ids=lambda m: m.name)
def test_every_benign_mutation_is_healed_AND_accepted(mutation, bench_evidence, history):
    # The complement of test_adversarial_refused. Refusing everything is safe
    # and worthless; this is the suite that says the product works.
    result = run_pipeline_on(bench_evidence[mutation.name], history=history)
    assert isinstance(result, ValidatedExtraction), verdicts_of(result)

@pytest.mark.parametrize("mutation", ALL_BENIGN_MUTATION_CLASSES, ids=lambda m: m.name)
def test_each_acceptance_names_the_confirmer_that_carried_it(mutation, bench_evidence, history):
    # An acceptance with no named confirmer is a check that silently passed.
    heal = heal_event_for(run_pipeline_on(bench_evidence[mutation.name], history=history))
    assert heal.confirmer_rung and heal.confirmer_family != heal.healed_family

def test_the_label_reword_case_specifically(bench_evidence, history):
    # C3. Before R14 this quarantines, forever, on the most common real UI change.
    result = run_pipeline_on(bench_evidence["LabelReword"], history=history)
    assert isinstance(result, ValidatedExtraction)
    assert heal_event_for(result).confirmer_family == "VALUE"

def test_the_quarantine_rate_on_benign_mutations_is_zero(bench_evidence, history):
    # The number that goes in the methodology next to the heal count.
    q = [m for m in ALL_BENIGN_MUTATION_CLASSES
         if not isinstance(run_pipeline_on(bench_evidence[m.name], history=history),
                           ValidatedExtraction)]
    assert q == [], f"benign mutations quarantined: {[m.name for m in q]}"
```

- [ ] **Step 2: Write the failing degraded-mode test**

```python
# tests/suite/test_degraded_mode.py
from bok_core.testing.degraded import blackout, assert_blackout_complete

def test_degraded_mode_still_produces_findings():
    # 00 R3 names this test by exactly this name; INTERFACE-CONTRACT.md section 5
    # sets this repo's floor and unit: "majority of mutation classes healed by
    # rungs 1-4". v1 makes zero model calls so it should be trivially green --
    # which is the point. It is a regression guard against a model dependency
    # creeping in, and design section 12 said to keep it. The plan had no such
    # test at all (M3).
    with blackout():
        healed = [m for m in ALL_BENIGN_MUTATION_CLASSES if heals_under_blackout(m)]
        assert len(healed) > len(ALL_BENIGN_MUTATION_CLASSES) / 2
        assert_blackout_complete()
```

- [ ] **Step 3: Run both and watch them fail.** Run: `uv run pytest tests/suite -v`

- [ ] **Step 4: Fix the gate until the acceptance suite passes.** If a benign mutation quarantines,
  that is a **gate defect**, exactly as a mutation that will not heal is a resolver defect. Read the
  named verdict before changing a threshold; the answer is usually a missing confirmer family, not a
  looser check.

- [ ] **Step 5: Record both rates in `docs/methodology.md`** — benign acceptance rate, adversarial
  refusal rate, and the degraded-mode floor with its unit. Three numbers, and they only mean something
  together: refusing everything scores 100% on the second and 0% on the first.

- [ ] **Step 6: Commit**

```bash
git add tests/suite docs/methodology.md
git commit -m "test: benign-heal acceptance suite and degraded-mode floor"
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

def test_claim_source_is_a_DomSource_carrying_the_snapshot_it_came_from(resolved, evidence):
    # H8/4. INTERFACE-CONTRACT 2.2 defines DomSource(snapshot_id, selector, rung)
    # specifically for this project. `snapshot_id` is the link from a claim back to
    # the stored evidence -- i.e. the entire point of the provenance rail here -- and
    # the first draft asserted only that the selector appeared somewhere in str(source).
    claim = read(resolved, SPEC, evidence).rows[0]["claim_number"]
    assert isinstance(claim.source, DomSource)
    assert claim.source.snapshot_id == evidence.sha256
    assert claim.source.selector == resolved.selector and claim.source.rung == resolved.rung_id

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
  - `write(validated: ValidatedExtraction) -> Path` — **accepts no other type**; one Parquet file per **attempt**, named `{run_key}-{attempt}-{ulid}.parquet`, into `lake/target=<t>/window=<w>/`
  - `query(sql: str)` — DuckDB over the partitioned tree, **never a shared file written by parallel flows**. The `lake` view deduplicates on `spec.natural_key`, keeping the row with the greatest `ingested_at`
  - `compact(target: str) -> None` — writes to a sibling directory and **swaps atomically**; triggered when a partition exceeds `COMPACT_AFTER_FILES` (default 24)

**The first draft's two idempotency requirements could not both hold (M6).** Design §9.1 says
*"a closed partition is never mutated in place"*, while Task 28's test writes the same
`ValidatedExtraction` twice and requires the row count not to change — which, for *"one Parquet file
per run"*, means overwriting that file, i.e. mutating in place.

**Append-only files plus a dedup view satisfies both**, and it is also what M13's `run_attempts`
needs: a retry writes a *new* file rather than erasing the failed attempt's output, and the view hides
the superseded rows. `natural_key` comes from the spec (Task 5, M5) — the phrase *"upsert on the
natural key"* appeared in the first draft with no field anywhere defining one.

`compact()` previously rewrote files under a live DuckDB glob with no atomicity and no trigger (L7,
P4), so a concurrent reader could hit a file that had just vanished.

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

def test_a_late_record_does_not_corrupt_a_closed_partition(validated, late_row, lake_dir):
    write(validated); write(with_row(validated, late_row))
    n = query("select count(*) from lake where window='2026-09-07'").fetchone()[0]
    assert n == len(validated.rows) + 1
    assert len(list(lake_dir.rglob("*.parquet"))) == 2   # appended, not overwritten (M6)

def test_a_retry_appends_a_file_and_the_view_hides_the_superseded_rows(validated, lake_dir):
    write(validated); write(with_corrected_value(validated))
    assert len(list(lake_dir.rglob("*.parquet"))) == 2
    assert query("select count(*) from lake").fetchone()[0] == len(validated.rows)
    assert query("select amount from lake limit 1").fetchone()[0] == CORRECTED

def test_compaction_is_atomic_under_a_concurrent_reader(validated, lake_dir):
    # L7. A reader must never glob a file that compaction has just unlinked.
    with concurrent_reader() as r:
        compact("testbench")
    assert r.errors == []

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
- Produces: `evaluate(run_key: str) -> list[Finding]` covering row count vs expectation, null rate, schema drift (against `spec.expected_schema`, Task 5/M4) and distribution shift.

**Use the contract's real field names (H8/3, M7).** `INTERFACE-CONTRACT.md` §4 defines
`Finding(id, severity, title, claim, taxonomy_refs, logical_location, partial_fingerprints,
properties)`. There is no `rule` attribute and no `detail` attribute; the first draft's tests asserted
on both. Rule name, threshold and threshold reason go in `properties`. **`claim` is required** — a
quality finding is a derived assertion and must carry the `Claim` chain it came from, which is also
what lets the UI open the provenance rail on it (M8).

- [ ] **Step 1: Write the failing tests**

```python
# tests/quality/test_contracts.py
from selector_drift.quality.contracts import evaluate

def test_row_count_shortfall_is_reported():
    fs = evaluate(run_with_half_the_expected_rows())
    assert any(f.properties["rule"] == "row_count" for f in fs)

def test_null_rate_spike_is_reported():
    assert any(f.properties["rule"] == "null_rate" for f in evaluate(run_with_null_spike()))

def test_schema_drift_is_reported_against_the_specs_expected_schema():
    # M4. The first draft tested schema drift against no baseline.
    assert any(f.properties["rule"] == "schema_drift" for f in evaluate(run_with_new_column()))

def test_every_finding_states_its_threshold_and_why():
    for f in evaluate(run_with_half_the_expected_rows()):
        assert f.properties["threshold"] and f.properties["threshold_reason"]

def test_every_finding_carries_the_claim_chain_it_was_derived_from():
    # INTERFACE-CONTRACT section 4: `claim` is required. Invariant I1: a DERIVED
    # claim's confidence may not exceed the minimum of its parents'.
    for f in evaluate(run_with_half_the_expected_rows()):
        assert f.claim.method is Method.DERIVED and f.claim.parents

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

### Task 29A: Alerting, the intervention ledger, and deploy history — R17 (M1, M2, M10)

**Files:**
- Create: `selector_drift/quality/alerts.py`, `selector_drift/orchestrate/interventions.py`, `selector_drift/orchestrate/deploys.py`
- Modify: `selector_drift/orchestrate/pipeline.py` (Task 26A)
- Test: `tests/quality/test_alerts.py`, `tests/orchestrate/test_interventions.py`

**Interfaces:**
- Consumes: `Finding` (Task 29), the `interventions` and `deploys` tables (Task 3).
- Produces:
  - `notify(findings, run) -> None` — one webhook. Fires on a quarantine, a `HIGH`+ finding, or a missed heartbeat. Deduplicates per `(target, rule)` per day so an alert is not a pager
  - `record_intervention(kind, actor, target, field, before, after, reason)` — called by the spec loader when a spec's hash changes between runs, by any manual cache edit, and by `promote()`
  - `ingest_deploys(target) -> int` — pulls deployment timestamps for the control-arm targets into `deploys`

**Why this task exists — three published claims currently have no evidence path.**

| Claim | Where it is made | What was missing |
|---|---|---|
| *"Alert **before** bad data reaches the client's system"* | brief §4, DoD 7 (*"contracts **and alerting**"*) | Task 29 produced `Finding` objects and nothing sent them anywhere. A quarantine at 04:12 notified nobody, so the log stalls silently (M2) |
| *"Zero manual selector fixes"* | design §8.3 — the headline | Nothing recorded a human editing a spec or a cache row, so the number was **unfalsifiable** — exactly the epistemic status of the vendor numbers `00` was written to reject (M1) |
| *"Heals fired only on runs following an actual deploy, verified against deploy history"* | design §8.1 — the control arm, *"the half nobody else has"* | No task ingested deploy history (M10) |

- [ ] **Step 1: Write the failing tests**

```python
def test_a_quarantine_notifies_exactly_once(quarantined_run, webhook):
    notify(*evaluate_and_run(quarantined_run)); notify(*evaluate_and_run(quarantined_run))
    assert len(webhook.calls) == 1                       # dedup per (target, rule) per day

def test_a_spec_edit_between_runs_is_recorded_as_an_intervention(session):
    edit_spec("specs/testbench.yaml", field="claim_number", description="the reference cell")
    extraction_flow("specs/testbench.yaml")
    row = session.query(Intervention).one()
    assert row.kind == "spec_edit" and row.before != row.after

def test_the_manual_fix_count_is_queryable_because_the_headline_depends_on_it(session):
    assert manual_fix_count(since=DAY_ONE) == session.query(Intervention).count()

def test_a_heal_on_a_control_target_is_joinable_to_a_deploy(session):
    ingest_deploys("agent-perimeter")
    heal = latest_heal("agent-perimeter")
    assert preceding_deploy(heal) is not None           # the control arm's whole claim
```

- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Implement.** The webhook target is an env var; a missing one logs loudly at startup rather than failing silently, because an alerting system nobody configured is worse than none (B7's cousin).
- [ ] **Step 4: Run the full check.**
- [ ] **Step 5: Commit**

```bash
git add selector_drift/quality/alerts.py selector_drift/orchestrate tests
git commit -m "feat(quality): alerting, intervention ledger, deploy-history ingest"
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
  - `store(evidence: Evidence, plane: Plane) -> str` — returns an `evidence_uri`. Order is **capture → scrub credentials → redact → verify → `egress.guard_write` → store**; on verification failure it raises `RedactionFailed`, writes nothing, and the caller quarantines the run with reason `evidence redaction failed`.
  - `scrub_credentials(dom: str, shot: bytes) -> tuple[str, bytes]` — **R15/H6**

**Three corrections against `INTERFACE-CONTRACT.md` (H8) and one gap it does not cover (H6).**

1. **`Evidence` carries `Labelled`, not a sibling `sensitivity` argument.** Contract §1.1:
   *"`Labelled` wraps bytes, `Path` and file handles, not only prompts. The label attaches where a
   file is opened. Attaching it at gateway entry is too late."* So `Evidence.dom: Labelled[str]` and
   `Evidence.screenshot: Labelled[bytes]`, labelled in `capture()` (Task 19), and the sensitivity
   comes from the `AuthorisedTarget`'s derived property (R4/H7) — never from a parameter a caller
   could get wrong.
2. **`redact` returns a `RedactionResult`, not a string.** Contract §1.3:
   `redact(payload: Labelled[str]) -> RedactionResult` with `verified: bool` —
   *"a field on a result the caller must destructure, never a bare bool that can be dropped."*
3. **The write goes through `egress.guard_write` (contract §1.5).** It is listed there as
   **"Required by `selector-drift`"** for this exact path, and *"consumers route their write paths
   through this rather than writing bespoke guards."* The first draft wrote a bespoke guard.
4. **Redaction looks for PII and misses every credential (H6).** `00` §4.2 scopes `redact` to
   *"emails, phones, tax ids, bank/routing numbers, addresses, person names"*. Evidence is a DOM
   snapshot and a screenshot **of a logged-in portal**: session cookies, `Authorization` headers,
   CSRF tokens in hidden inputs, and the contents of `input[type=password]` if a login page is
   captured mid-flow. None of these look like PII, so none are redacted — the highest-value secret in
   the system is the one thing the pass does not look for. `scrub_credentials` runs **first**, over
   password and hidden input values, any `Set-Cookie` / `Authorization` text, and anything matching
   the storage-state shape.
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

def test_redaction_failure_writes_nothing(leaky_evidence, evidence_root):
    # Un-redacted evidence is never written to be cleaned up later. There is no later.
    # NOTE the fixture: the first draft asserted on a `tmp_path` that `store()` was
    # never told about, so it passed whatever store() actually did (M19).
    with pytest.raises(RedactionFailed):
        store(leaky_evidence, Plane.CLIENT)
    assert not list(evidence_root.rglob("*"))

def test_no_session_cookie_survives_into_stored_evidence(logged_in_evidence):
    # R15/H6. A portal DOM snapshot is taken while authenticated.
    uri = store(logged_in_evidence, Plane.CLIENT)
    blob = Path(uri).read_bytes()
    assert SESSION_COOKIE_VALUE.encode() not in blob
    assert CSRF_TOKEN.encode() not in blob

def test_a_password_field_value_never_reaches_the_snapshot(login_page_evidence):
    uri = store(login_page_evidence, Plane.CLIENT)
    assert b"hunter2" not in Path(uri).read_bytes()

def test_client_confidential_evidence_cannot_be_written_to_the_demo_plane(client_evidence):
    # Contract 1.5. This is the check guard_write exists to make, and the reason
    # it is not a bespoke local guard.
    with pytest.raises(SensitivityViolation):
        store(client_evidence, Plane.DEMO)

def test_stored_evidence_contains_no_original_identifier(evidence_with_pii):
    uri = store(evidence_with_pii, Sensitivity.CLIENT_CONFIDENTIAL)
    assert "Jane Okafor" not in Path(uri).read_text()

def test_synthetic_evidence_is_still_redacted_before_storage(bench_evidence):
    # Design 8.2 says a SYNTHETIC target's evidence is publishable with no redaction
    # argument -- true, and exactly why redaction must NOT be conditional on the label
    # (H7). Redact unconditionally; publish from an explicit allowlist, never from a
    # string in a YAML file that one typo could change.
    uri = store(bench_evidence, Plane.DEMO)
    assert uri and was_redacted(uri)
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

**Every route authenticates, and `actor` is never read from a request body (R16/H3).** The first
draft had no authentication of any kind — `GET /heals/{id}` serves redacted evidence and DOM
snapshots, `GET /quality/{run_key}` serves extracted rows — and `promote` took its `actor` from the
caller. DoD item 4 is *"quarantine with promote/discard and **full audit**"*, and the whole sales
argument is that a named human confirmed a heal: **an audit log whose `actor` is supplied by the party
being audited is not an audit log.**

v1 is single-tenant and local, so the cheapest thing that is actually a control:

- session cookie auth, one operator account, `HttpOnly` + `SameSite=Strict`, argon2id password hash;
- `actor` comes from `Depends(current_user)`, and the request models **do not declare an `actor`
  field**, so anyone sending one gets a `422`;
- path parameters (`run_key`, `id`, `target`) validated against a pattern before use, since they index
  `evidence_uri` and `data_uri` on disk;
- CORS locked to the web origin; the API is not published outside the compose network by default.

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_endpoints.py
import pytest

@pytest.mark.parametrize("path", ["/targets", "/runs", "/heals/1", "/quality/k", "/specs/testbench"])
def test_get_endpoints_return_200(client, path):
    assert client.get(path).status_code == 200

def test_every_route_requires_authentication(anon_client):
    # H3. These routes serve redacted evidence and extracted client rows.
    for path in ["/targets", "/runs", "/heals/1", "/quality/k", "/specs/testbench"]:
        assert anon_client.get(path).status_code == 401

def test_promote_requires_a_reason_and_takes_its_actor_from_the_session(client):
    assert client.post("/quarantine/k/promote", json={}).status_code == 422
    client.post("/quarantine/k/promote", json={"reason": "checked the screenshots"})
    assert latest_audit().actor == client.session_user      # not from the body

def test_a_caller_supplied_actor_is_rejected_outright(client):
    r = client.post("/quarantine/k/promote", json={"reason": "x", "actor": "someone-else"})
    assert r.status_code == 422

def test_a_traversing_run_key_cannot_reach_a_file(client):
    assert client.get("/quality/..%2f..%2fetc%2fpasswd").status_code in (400, 404, 422)

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
  // L3: bounded. An unbounded Tab loop hangs to the suite timeout instead of failing.
  let found = false;
  for (let i = 0; i < 60 && !found; i++) {
    await page.keyboard.press('Tab');
    found = await page.evaluate(() => !!document.activeElement?.textContent?.includes('Promote'));
  }
  expect(found, 'Promote never received focus within 60 tab stops').toBe(true);
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

### Task 39A: The cross-cutting UI requirements `00` mandates — R17 (M8, M11)

**Files:**
- Create: `web/components/ClaimValue.tsx` (wraps `bok-ui` `Claim` + `ProvenanceRail`), `web/components/CommandPalette.tsx`
- Create: `web/app/print.css`
- Modify: `web/app/heals/[id]/page.tsx`, `web/app/quality/page.tsx`, `web/app/quarantine/page.tsx`
- Test: `web/e2e/provenance.spec.ts`, `web/e2e/print.spec.ts`, `web/e2e/responsive.spec.ts`

**Why this task exists.** Five requirements from `00` §5 apply to every product in the programme and
none of them appears anywhere in Tasks 35–39 (verified: `grep -ic` for `ProvenanceRail`,
`provenance rail`, `cmdk`, `375` all return 0). Design §10 makes an explicit and well-argued scope
call on screens 2 and 5; it makes none on these, so they read as forgotten rather than cut — and one
of them is the programme's signature element.

| Requirement | Source | Why it is not optional here |
|---|---|---|
| **Provenance rail** on every rendered figure | `00` §5.3: *"the signature element, present in all four products, and the reason a buyer remembers which vendor you were"*; contract §7 lists `Claim` + `ProvenanceRail` among this repo's consumed components | Task 27 turns every extracted value into a `Claim` with a `DomSource` carrying `snapshot_id`, and no screen renders one. Clicking an extracted value and watching it unfold to *the DOM snapshot and the rung that produced it* is this product's version of the demo `00` §5.3 describes |
| **Print stylesheet** | `00` §5.5: *"for every report view"* | The heal log **is** the report view. It gets printed and forwarded to auditors; that is the buyer behaviour `00` §5.1 designs the whole system around |
| **`cmdk` command palette** | `00` §5.5: *"full keyboard operation including a `cmdk` command palette"* | — |
| **Responsive to 375px** | `00` §5.5 | — |
| **Density modes** | `00` §5.2, contract §6.1 ships `comfortable`/`compact` in v0.1 | Free from `bok-ui`; needs wiring only |

- [ ] **Step 1: Write the failing tests**

```typescript
// web/e2e/provenance.spec.ts
test('an extracted value opens its provenance chain', async ({ page }) => {
  await page.goto('/heals/1');
  await page.getByTestId('claim-value').first().click();
  const rail = page.getByTestId('provenance-rail');
  await expect(rail).toBeVisible();
  await expect(rail).toContainText('DETERMINISTIC');
  await expect(rail.getByTestId('source')).toContainText(/rung [1-4]/);
  await expect(rail.getByTestId('source')).toContainText('snapshot');   // DomSource
});

test('a deterministic claim shows no confidence figure at all', async ({ page }) => {
  // Invariant I3 + design 5.4: there is no confidence score in this product,
  // so the UI must not imply one exists but is hidden.
  await page.goto('/heals/1');
  await page.getByTestId('claim-value').first().click();
  await expect(page.getByTestId('confidence')).toHaveCount(0);
});
```

```typescript
// web/e2e/print.spec.ts
test('the heal log prints without the chrome and keeps the verdict table', async ({ page }) => {
  await page.goto('/heals/1');
  await page.emulateMedia({ media: 'print' });
  await expect(page.getByTestId('nav')).toBeHidden();
  await expect(page.getByTestId('verdict-row').first()).toBeVisible();
});
```

```typescript
// web/e2e/responsive.spec.ts
for (const route of ROUTES) {
  test(`${route} does not scroll horizontally at 375px`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 });
    await page.goto(route);
    expect(await page.evaluate(() =>
      document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
```

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Build the four pieces.** `ClaimValue` wraps `bok-ui`'s `Claim` and rail; the palette is
  `cmdk` with the six routes plus promote/discard; `print.css` is the standalone CSS the contract ships
  in `bok-ui` v0.1 (§6.1), imported not reimplemented.

- [ ] **Step 4: Add the remediation link the quarantine screen is missing (M11).**

  After a *permanent* UI change — a label renamed for good — the steady state of this product is a
  nightly quarantine, because screen 2 is read-only in v1 by design. The operator's only route is
  editing the spec YAML by hand, and nothing in the UI says so. That is the daily work of the
  $1–5k/month maintenance retainer, and it currently has no documented path. Add to each quarantined
  run: the field, the current spec excerpt, and a link to `docs/runbook-spec-update.md` — edit the
  target's `description`, bump `spec_version` (which invalidates the cache by M14's PK), re-run,
  confirm the first heal.

- [ ] **Step 5: Run them and make sure they pass.** Run: `cd web && npx playwright test`

- [ ] **Step 6: Commit**

```bash
git add web docs/runbook-spec-update.md
git commit -m "feat(web): provenance rail, print stylesheet, palette, 375px, spec runbook"
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
            "deploy history", "first-heal regime",
            # added by the audit -- each is a number the log publishes, or a caveat
            # a reader needs to interpret one
            "benign acceptance rate",     # 26B -- refusing everything scores 100% on
            "adversarial refusal rate",   #        refusal and 0% on acceptance
            "degraded-mode floor",        # 00 R3 / contract section 5, with its unit
            "manual interventions",       # M1 -- "zero manual fixes" needs a denominator
            "psi binning",                # M16 -- 10 quantile bins fixed from the reference
            "reference window",           #        window; otherwise PSI 0.03 is not checkable
            "days elapsed",               # honest count at week 20, not "90 days"
            "missed runs",                # B3 -- gaps are stated, not hidden by the chart
            "retention"]                  # 00 B7

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

### Task 40A: Publish the heal log — R17 (M9)

**Files:**
- Create: `selector_drift/heallog/publish.py`, `.github/workflows/publish-heallog.yml`
- Create: `docs/heallog/index.md`, `docs/disclosure.md`
- Test: `tests/heallog/test_publish.py`

**Interfaces:**
- Consumes: `runs`, `run_attempts`, `resolutions`, `heal_events`, `interventions`, `deploys`, evidence storage.
- Produces: a static, versioned public artifact — the thing the whole phase exists to produce.

**Why this task exists.** DoD 6 is *"ninety-day heal log running since day one, with **published**
methodology"*, and design §8.2 argues at length about publishability — but **no task produced the
artifact.** The plan generated the data and stopped. `00` B9 also attaches obligations to publishing
that only the methodology half was covering.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_published_log_contains_no_client_confidential_evidence(published):
    # Design 8.2, H7. Publication is gated on an explicit allowlist of targets,
    # NOT on a `sensitivity` string a typo could change. A client target added
    # later contributes COUNTS ONLY -- no screenshots, no DOM, ever.
    assert set(published.targets_with_evidence) <= PUBLISHABLE_TARGETS
    for t in published.client_targets:
        assert published.evidence_for(t) == [] and published.counts_for(t)

def test_every_published_number_is_computed_not_typed(published):
    assert published.numbers == recompute_from_database()

def test_the_log_states_its_elapsed_days_not_ninety(published):
    # B10 -- a partial log with an honest start date beats no log, but only if it
    # says which it is.
    assert published.elapsed_days == actual_days_since(DAY_ONE)

def test_the_log_states_missed_runs_and_manual_interventions(published):
    assert "missed" in published.summary and published.manual_interventions is not None

def test_a_non_reproducible_heal_is_published_as_such(published):
    # B5. A heal you cannot reproduce is an anecdote; the log says so rather than
    # quietly counting it.
    assert all(h.replayed_ok is not False or h.flagged for h in published.heals)

def test_results_are_versioned_and_carry_a_changelog(published):
    assert published.version and Path("docs/heallog/CHANGELOG.md").exists()
```

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Implement `publish.py`** — reads the tables, renders static HTML/Markdown to
  `docs/heallog/`, deployed by GitHub Pages (free, already in the template). No database, no server;
  design §2.2 Q4's *"the published heal log is a static export with no database"*.

- [ ] **Step 4: Write `docs/disclosure.md`** — `00` B9: a published artifact making claims about
  software needs a stated sample and population, a coordinated-disclosure route, a right of reply, and
  versioned results with a changelog. The bench is ours and `agent-perimeter` / `ledger-sense` are
  ours, so the right-of-reply surface is small — which is the argument for writing it now, while it
  costs nothing, rather than after a client target joins.

- [ ] **Step 5: Schedule it weekly** and re-run at day 90.

- [ ] **Step 6: Commit**

```bash
git add selector_drift/heallog/publish.py docs/heallog docs/disclosure.md .github/workflows
git commit -m "feat(heallog): published static heal log with disclosure policy"
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

def test_dod_03a_every_adversarial_mutation_is_refused():
    assert all(is_quarantined(m) for m in ADVERSARIAL_MUTATIONS)   # all seven (Task 11A)

def test_dod_03b_every_benign_mutation_is_accepted():
    # C3. Without this, a gate that refuses everything passes 03a and the product
    # does not work. The two numbers only mean anything together.
    assert all(is_written(m) for m in BENIGN_MUTATIONS)

def test_dod_03c_degraded_mode_floor_holds():
    assert pytest_passed("tests/suite/test_degraded_mode.py")      # 00 R3, M3

def test_dod_04_quarantine_promote_discard_audited():
    assert audit_rows_for("promote") and audit_rows_for("discard")

def test_dod_05_testbench_shipped_and_documented():
    assert Path("testbench/README.md").exists()
    assert all(m.name in Path("testbench/README.md").read_text() for m in ALL_MUTATIONS)

def test_dod_06_log_running_since_day_one():
    first = query("select min(first_started_at) from runs").fetchone()[0]
    elapsed = (dt.datetime.now(dt.UTC).date() - first.date()).days
    assert elapsed >= 1          # raise to 90 at day 90
    # M19: `>= 1` is nearly vacuous, so assert continuity too -- a log with holes
    # is the thing B3 is about, and the artifact shows them.
    assert missed_scheduled_runs(since=first) <= ALLOWED_GAPS

def test_dod_07_landing_zone_idempotent_under_replay():
    before = query("select count(*) from lake").fetchone()[0]
    replay_last_run()
    assert query("select count(*) from lake").fetchone()[0] == before

def test_dod_07b_quality_findings_reach_the_alert_channel():
    assert webhook_calls_for(last_quarantined_run())                # M2, DoD 7 "and alerting"

def test_dod_11_the_pipeline_actually_runs_end_to_end():
    # C1. Every other assertion in this file reads tables that only the assembled
    # pipeline fills. Without Task 26A they were all reading an empty database.
    assert pytest_passed("tests/orchestrate/test_pipeline_end_to_end.py")

def test_dod_12_a_quarantined_heal_never_reaches_the_selector_cache():
    # C2. The one ordering bug that would make every later run silently wrong.
    assert pytest_passed("tests/resolve/test_r1_cache.py::test_a_quarantined_heal_does_not_update_the_cache")

def test_dod_13_the_browser_cannot_leave_the_authorised_origin():
    assert pytest_passed("tests/browser/test_driver.py::test_navigation_off_the_authorised_origin_is_aborted")

def test_dod_14_the_rate_limit_is_enforced_and_persistent():
    # brief 3.1 and 11, design 4.3 and 12 -- required in four places, tested in none
    # until this revision (H4).
    assert pytest_passed("tests/browser/test_driver.py::test_the_limiter_is_persistent_across_processes")

def test_dod_15_the_api_refuses_an_anonymous_caller():
    assert pytest_passed("tests/api/test_endpoints.py::test_every_route_requires_authentication")

def test_dod_16_the_published_log_exists_and_states_its_elapsed_days():
    assert pytest_passed("tests/heallog/test_publish.py")           # M9

def test_dod_17_compose_up_is_the_only_setup_step():
    # 00 section 3.5, a hard constraint that nothing else in the plan verified.
    assert fresh_machine_compose_up_succeeds()

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
- **The log matures after the code does.** Day one to the end of build week 8 is 56 days. Publish the partial log at week 20 with its honest elapsed count, and update it at day 90 — Task 41's `test_dod_06` carries a `>= 1` assertion for week 20 and a comment to raise it to `>= 90` on the day. A partial log with an honest start date beats no log (B10).
- **Phase B before Phase C.** You cannot test a resolver against pages that do not change.
- **Task 17 after Tasks 14–16.** Rung 1 stores anchor evidence produced by whichever rung healed, so those rungs must exist first.
- **Task 22 is the task that justifies the project.** If only one thing in this plan is done carefully, it is cross-family confirmation — now including the `VALUE` family, without which every label rewording quarantines forever (C3).
- **The day-one schema is the day-one schema.** Task 3 freezes it for the reason it states, which is exactly why the four tables the audit added (`resolutions`, `run_attempts`, `deploys`, `interventions`) and the two key changes are not deferrable. Three published numbers — the rung distribution, the run-success rate and the manual-fix count — have no source without them, and a table missing on day one is missing for the whole log.
- **`PageSource` (Task 13) comes before any rung.** It is not tidiness. It is what makes replay legal under the import contracts, what lets the mutation suite run without a browser, and what turns hundreds of CDP round-trips per run into one snapshot. Retrofitting it after Tasks 14–17 means rewriting all four rungs.
- **Task 26A is not optional and has no substitute.** Before it, nothing connects resolve → gate → land. Every assertion in Task 41 reads a table that only the assembled pipeline fills.
- **Task 26B measures the half that decides whether the product works.** Task 26 shows the resolver can heal and the gate can refuse. 26B shows the gate does not refuse everything. A system that quarantines every legitimate heal passes 26 and fails the customer.

## Revision log

Applied 2026-08-29 from [`docs/superpowers/reviews/2026-08-29-plan-audit.md`](../reviews/2026-08-29-plan-audit.md).
Each edit carries its finding id inline, so the reasoning is readable at the point of change rather
than only in the audit.

| New task | Fixes |
|---|---|
| **11A** | H9, M25 — six further adversarial mutation classes; visibility precondition |
| **26A** | C1 — assembles the pipeline; fixes the `cache.put` ordering (C2) at the call site |
| **26B** | C3, M3 — benign-heal acceptance suite; degraded-mode floor |
| **29A** | M1, M2, M10 — alerting, intervention ledger, deploy-history ingest |
| **39A** | M8, M11 — provenance rail, print CSS, palette, 375px, spec-update runbook |
| **40A** | M9 — the published heal log and its disclosure policy |

Substantive amendments to existing tasks: 1 (deps, contracts, scaffold), 2 (record integrity, mint
identity, derived sensitivity), 3 (**four new tables, frozen schema**), 4 (**origin allowlist, rate
limiter, container hardening**), 5 (row model, expected schema, safe loading), 6 (rung-stats source,
attempts, host, alert stub), 8 (**one mutation engine**), 12 (working verification), 13
(**`PageSource`, `node_id`, visibility**), 17 (**cache write-back gated on the validated type**), 19
(replay via `StaticPageSource`), 21 (safe evaluator), 22 (**`Family.VALUE`**), 23 (PSI specified,
persistence split, per-target warm-up), 25 (patch target, actor), 27 (`DomSource`), 28 (append-only
lake), 29 (contract field names), 33 (`Labelled`, `guard_write`, credential scrubbing), 34
(**authentication**), 39 (bounded loop), 40 (methodology completeness), 41 (seven further DoD tests).

**Audit Open Questions 1–4 were decided 2026-08-29 and are applied above:**

| OQ | Decision | Applied in |
|---|---|---|
| 1 | **Runs out of programme order, first.** All week numbers are project-relative; no calendar date is assumed. The control arm's targets are superseded and re-named in P-3 | §Week numbering, P-3, design §2.1 Q1 / §8.1 |
| 2 | **Free always-on VM** (Oracle Always Free / Fly.io), which also hosts the bench | P-4, Task 6 Step 5, design §9.2 |
| 3 | **Take the `Family.VALUE` amendment.** The conservative alternative is rejected — daily re-seeding makes "one confirmation" a nightly one | Task 22, design §5.6a |
| 4 | **Bench moves off Vercel** onto the P-4 VM | Task 12 Step 1, design §7 |

**One thing is still open and only you can close it: P-3.** Two self-owned, deployed applications must
carry the control arm in place of `agent-perimeter` and `ledger-sense`. Until they are named, Task 5's
scope records, Task 6's target list and design §8.1's control-arm claim carry placeholder names.
