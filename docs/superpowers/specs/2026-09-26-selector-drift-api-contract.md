# Selector Drift — API contract and evidence bundle

**Status:** approved in brainstorming 2026-09-26 · **Parent:** [design](2024-11-07-selector-drift-design.md) §3.3
**Plan tasks:** 2 (`evidence_retention_days`), 3 (`operators`, `sessions`, `heartbeats`), 6, 19, 26A, 33 (evidence bundle), 34, 34B (`derive.py`), 40A
**Siblings:** [web UI](2026-09-26-selector-drift-web-ui.md) · [test bench](2026-09-26-selector-drift-testbench-portal.md) · [`bok-ui` slice](2026-09-26-selector-drift-bok-ui-slice.md)

This is the hinge between the pipeline and every surface that shows its output: the operator UI,
the published static heal log (plan Task 40A), and E2E fixtures. It was written first so that the UI
spec and the publisher are written *against* it rather than each inventing a shape for the same heal
record.

---

## 1. Three rules

1. **Read models are projections of the frozen day-one tables** (plan Task 3). No route returns a
   field with no column behind it. A value that must be computed — elapsed days, rung shares,
   run-success rate — is computed in exactly one module, `selector_drift/api/derive.py` (§6), which
   Task 40A's static publisher also imports. The operator screen and the public log therefore cannot
   disagree about a number.
2. **Every extracted value crosses the wire as a rendered `Claim`** — `bok_core.provenance.render.to_rail()`
   output plus the raw value — never a bare string. That is what lets any extracted figure on any
   screen open the provenance rail. Aggregates are the exception, and why is in §6.2.
3. **Every check result crosses the wire as a `Verdict`** — `check`, `outcome`, `threshold`,
   `observed`, `reason`. There is no boolean field anywhere in the API that means "passed". Brief §8
   screen 5 forbids bare red badges; a payload with no bare booleans gives the UI nothing bare to
   render.

---

## 2. Layout

```
selector_drift/api/
  main.py          app factory; mounts routers under /api/v1; problem+json handlers
  auth.py          login/logout/me, session issue + verify, argon2id, login throttle
  deps.py          current_operator, db session, path-parameter validators
  errors.py        ProblemDetail, the exception -> status map (§5)
  views.py         every response model (§4) — the only module the type generator reads
  derive.py        every computed figure (§6) — shared with heallog/publish.py
  evidence.py      bundle read path: manifest verify, stream, corrupted/expired detection
  routers/
    targets.py  runs.py  heals.py  quarantine.py  quality.py  specs.py  stats.py
```

`api/` may import `db/`, `heallog/` (read side), `validate/` (types and `quarantine`),
`spec/`, `scope/` (read-only loaders — never `authorise()`), and **`resolve/trace.py` only** — for the
plain types `Family` and `AnchorEvidence` that `views.py` re-exports. It must not import `browser/`,
the rest of `resolve/`, `land/`, or `orchestrate/`: the API reports on runs, it never starts one.
New import-linter contract `api-is-read-side`.

**Promotion lands data, and the API may not import `land/` or `orchestrate/` — resolved by
injection (2026-09-26).** `create_app(actions: QuarantineActions)` receives the promote/discard
implementation built in `orchestrate/` (which may import everything); `python -m selector_drift
serve-api` wires it. The API calls `actions.promote(run_key, actor, reason)`; the protocol lives in
`api/`, the implementation does not. `validate.quarantine.promote()` re-runs the gate and returns the
`ValidatedExtraction`; `orchestrate` lands it through the same `accept()` a scheduled run uses.

---

## 3. Authentication and the audit actor

v1 is single-tenant with one or a few operator accounts. The cheapest thing that is actually a
control (closes audit H3):

| Concern | Decision |
|---|---|
| Accounts | `operators` table. Created by `python -m selector_drift create-operator <username>`, which prompts for the password on a TTY (never an argument, never an env var). No self-registration route exists |
| Password | argon2id (`argon2-cffi`), library defaults |
| Session | 32 random bytes, sent as cookie `sd_session`, `HttpOnly; Secure; SameSite=Strict; Path=/`. **Stored as its SHA-256** in `sessions`, so a database read does not yield a usable cookie. Absolute lifetime 12 h, no sliding renewal |
| CSRF | `SameSite=Strict` plus an `Origin` check on every non-GET: the header must equal the configured web origin, else `403` |
| Brute force | 5 failed logins per username per 15 min → `429` with `Retry-After`. Failures are counted in the audit log, not in memory, so a restart does not reset the count |
| Actor | Always `current_operator.username` from `Depends(current_operator)`. **No request model declares an `actor` field**, and every request model sets `extra="forbid"`, so a body carrying `actor` gets `422`. An audit trail whose actor is supplied by the party being audited is not an audit trail |
| Audit | login success, login failure, logout, promote, discard → `audit_log` rows |

**Two tables join the day-one schema (plan Task 3).** `operators(id, username UNIQUE, password_hash,
created_at, disabled_at)` and `sessions(token_sha256 PK, operator_id FK, created_at, expires_at,
revoked_at)`. The freeze rule exists so day 1 and day 90 are comparable. These tables hold no log
data, so they could be added later without breaking that comparison. They go in on day one anyway
because Task 3 has not been implemented yet and adding them now costs nothing.

---

## 4. Resources

All routes are under `/api/v1`, all require a session except `POST /auth/login`, all return
`application/json` or `application/problem+json`, and all request models are `extra="forbid"`.

### 4.1 Path parameters are validated before they touch anything

`run_key` indexes files on disk (`data_uri`, lake partitions) and `id` indexes evidence bundles. Every
path parameter is matched against a pattern in `deps.py` **before** any lookup:

| Parameter | Pattern |
|---|---|
| `target` | `^[a-z0-9][a-z0-9-]{0,62}$` |
| `run_key` | `^[a-z0-9-]{1,63}:\d{1,6}:\d{4}-\d{2}-\d{2}$` (the Task 6 `run_key()` format) |
| heal `id` | integer, `> 0` |
| evidence `side` | `before` \| `after` · `kind` `dom` \| `png` |

A non-matching parameter is `422`, never a lookup. `test_a_traversing_run_key_cannot_reach_a_file` stays.

### 4.2 Routes

| Method + path | Response model | Notes |
|---|---|---|
| `POST /auth/login` | `OperatorView` + `Set-Cookie` | body `{username, password}` |
| `POST /auth/logout` | `204` | revokes the session row |
| `GET /auth/me` | `OperatorView` | `401` if no session |
| `GET /targets` | `TargetView[]` | union of loaded scope records and spec `scope_ref`s; see §4.3 |
| `GET /runs` | `Page[RunView]` | query `target`, `from`, `to` (UTC dates), `cursor`, `limit ≤ 200` |
| `GET /runs/{run_key}` | `RunDetailView` | attempts, per-field resolutions, heal ids, quality finding count |
| `GET /heals/{id}` | `HealView` | |
| `GET /heals/{id}/evidence/{side}/{kind}` | `text/html` or `image/png` stream | §7.3. `Cache-Control: private, no-store`; `Content-Security-Policy: sandbox` on `dom` |
| `GET /heals/{id}/diff` | `DiffSpan[]` | Task 33 `dom_diff(before.html, after.html)` |
| `GET /quarantine` | `QuarantineView[]` | unresolved rows only; `?resolved=true` for history |
| `POST /quarantine/{run_key}/promote` | `QuarantineView` | body `{reason: str (1–500)}`; `409` `still-invalid` carries the failing `Verdict[]` |
| `POST /quarantine/{run_key}/discard` | `QuarantineView` | body `{reason}` |
| `GET /quality` | `Page[FindingView]` | query `run_key` or `target`; reads the `findings` table (§4.4) |
| `GET /specs/{target}` | `SpecView` | raw YAML text + `validation: Verdict[]` |
| `GET /stats` | `StatsView` | everything from `derive.py`; query `target`, `from`, `to` |

The API never serves the dangerous half of an evidence bundle to a browser as a document. `dom` is
served with `Content-Security-Policy: sandbox` and `X-Content-Type-Options: nosniff`, and the UI
renders it as escaped text in `EvidencePane`, never in an `iframe` with scripts allowed. The HTML is
a hostile page's markup.

### 4.3 Response models (`views.py`)

```python
class RateLimitView(BaseModel):
    requests: int
    per_seconds: int

class TargetView(BaseModel):
    target: str
    status: Literal["active", "expired", "unscoped"]
    locked_reason: str | None               # set iff status != "active"; words, never a code
    basis: Basis | None                     # None iff unscoped
    sensitivity: Sensitivity | None         # DERIVED from basis (R4) — displayed, never editable
    authorising_party: str | None
    authorised_on: date | None
    expires_on: date | None
    evidence_uri: str | None
    evidence_sha256: str | None
    origins: list[str]
    rate_limit: RateLimitView | None
    user_agent_contact: str | None
    first_heal_policy: FirstHealPolicy | None
    warmup_runs: int | None
    evidence_retention_days: int | None     # None = kept for the life of the log (self-owned only)
    legal_review: str | None
    record_sha256: str | None
    loaded_at: datetime | None

class RunView(BaseModel):
    run_key: str
    target: str
    spec_version: int
    window: date
    state: Literal["completed", "healed", "quarantined", "failed", "refused"]  # the frozen five
    quarantine_resolution: Literal["promoted", "discarded"] | None
    attempts: int
    first_started_at: datetime
    last_finished_at: datetime | None
    rows_written: int | None
    error: str | None

class AttemptView(BaseModel):
    attempt: int
    started_at: datetime
    finished_at: datetime | None
    state: str
    error: str | None
    scope_record_sha256: str

class RungAttemptView(BaseModel):
    rung: int
    family: Family
    outcome: Literal["resolved", "no_match", "ambiguous", "not_visible", "anchor_mismatch", "escalated"]
    ms: int

class ResolutionView(BaseModel):
    field: str
    winning_rung: int | None                # None: no rung resolved
    attempts: list[RungAttemptView]
    node_id: str | None
    heal_id: int | None

class RunDetailView(RunView):
    run_attempts: list[AttemptView]
    resolutions: list[ResolutionView]
    finding_count: int

class DecisionView(BaseModel):
    # Derived, not stored as one column:
    #   "accepted"  — the gate passed with no human (runs.state == healed, no quarantine row);
    #                 includes a first heal passed by first_heal_policy: auto_promote
    #   "promoted"  — quarantine.resolved_as == promoted (heal_events.promoted_by is the actor)
    #   "discarded" — quarantine.resolved_as == discarded
    #   None        — still quarantined, awaiting a decision
    resolved_as: Literal["accepted", "promoted", "discarded"] | None
    actor: str | None                       # None for "accepted": no human decided
    at: datetime | None
    reason: str | None

class BBox(BaseModel):
    x: float; y: float; width: float; height: float   # CSS px in the screenshot's coordinate space

class EvidenceSideView(BaseModel):
    available: bool
    unavailable_reason: Literal["corrupted", "expired", "not_captured"] | None
    captured_at: datetime | None
    viewport: tuple[int, int] | None
    boxes: dict[str, BBox]                  # field name -> recorded box (§7.2); may be empty
    dom_url: str | None                     # /api/v1/heals/{id}/evidence/{side}/dom
    png_url: str | None

class ExtractedValueView(BaseModel):
    field: str
    row_key: dict[str, str]                 # natural-key values identifying the row
    value: str
    rail: RailPayload                       # bok_core.provenance.render.to_rail(claim) — DomSource

class HealView(BaseModel):
    id: int
    run_key: str
    target: str
    field: str
    broke_at: datetime
    winning_rung: int
    confirmer_rung: int | None              # None when the confirmer was the VALUE family
    confirmer_family: Family | None         # None: no cross-family confirmer resolved
    selector_before: str | None
    selector_after: str
    anchor_before: AnchorEvidence | None
    anchor_after: AnchorEvidence
    trace: list[RungAttemptView]
    verdicts: list[Verdict]
    replay_status: Literal["reproduced", "not_reproducible", "not_yet_replayed", "corrupted"]
    before: EvidenceSideView
    after: EvidenceSideView
    sample_values: list[ExtractedValueView] # first 10 rows of the healed field
    decision: DecisionView
    summary: str                            # the design §5.4 heal-log sentence, built in derive.py

class QuarantineView(BaseModel):
    run_key: str
    target: str
    reason: str
    verdicts: list[Verdict]
    failing: list[Verdict]                  # verdicts with outcome FAIL — what still refuses after a
                                            # human confirms; INSUFFICIENT is tolerated once confirmed
                                            # (design §5.9), so it is not "failing" (amended 2026-09-26)
    preview: list[dict[str, ExtractedValueView]]   # first 20 rows
    heal_ids: list[int]
    spec_excerpt: str                       # the FieldTarget YAML for each failing field (M11)
    runbook_url: str                        # docs/runbook-spec-update.md
    decision: DecisionView

class FindingView(BaseModel):              # bok_core Finding, flattened for display
    id: str
    run_key: str
    severity: Severity
    title: str
    check: str                              # properties["rule"]
    observed: str
    threshold: str
    threshold_reason: str                   # required — brief §8 screen 5
    rail: RailPayload

class SpecView(BaseModel):
    target: str
    version: int
    yaml: str
    validation: list[Verdict]

class StatsView(BaseModel):                 # §6.2 — plain figures with a method link, not Claims
    window: tuple[date, date]
    elapsed_days: int
    scheduled_runs: int
    missed_runs: int
    fired_without_attempt: int
    run_success_rate: float                 # completed+healed / attempted, from run_attempts
    rung_shares: dict[str, float]           # rung1..rung5, from resolutions; {} before the first
    heals: int
    quarantines: int
    quarantines_refused_correctly: int | None   # bench only; None for targets without an answer key
    manual_interventions: int               # manual SELECTOR fixes: spec_edit (except field
                                            # "schedule") and cache_edit interventions
    human_confirmations: int                # manual_promote interventions: first-heal confirmations,
                                            # reported beside the fixes, never counted as one
    method_url: str                         # docs/methodology.md anchor for each figure

class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None                 # opaque; base64 of (first_started_at, run_key)

class OperatorView(BaseModel):
    username: str
```

`Verdict`, `Family`, `AnchorEvidence`, `Basis`, `FirstHealPolicy` are imported from their owning
packages, not redeclared, so a change to them is a change to the contract and the type generator sees
it.

**`quarantines_refused_correctly` is computed by the scorer, not by `api/`.** It needs the answer key,
which `selector_drift/` may not import (design §3.4). The bench scorer (`testbench/score.py`) writes
its per-heal judgement into `audit_log` as `action="scored"`, `actor="testbench.score"`,
`subject=str(heal_id)`, `detail={"judgement", "field", "run_key", ...}`; `derive.py` counts those rows
by actor and date window, never by parsing `subject`. The answer key stays unreachable from the
product.

**`manual_interventions` and `human_confirmations` are two figures (amended 2026-09-26).** The
headline claims zero manual *selector fixes*; a first-heal confirmation is the designed human step
(design §5.8) and a schedule edit changes when, not what. Counting either as a fix would make the
headline false for the wrong reason, so `manual_interventions` counts `spec_edit` (except
`field="schedule"`) and `cache_edit` rows, and `human_confirmations` counts `manual_promote` rows.

### 4.4 A `findings` table joins the day-one schema (added 2026-09-26)

Task 29's `evaluate()` returns `list[Finding]` and nothing stored them, so `GET /quality` and
`RunDetailView.finding_count` had no column behind them — a breach of rule 1 (§1). The frozen schema
gains `findings(id PK text, run_key FK, severity, rule, payload JSONB, at)`, indexed on `run_key`;
`payload` is the whole `Finding` as `model_dump(mode="json")`, so the `Claim` chain it carries is
kept. The ORM class is `FindingRow`, not `Finding`, which is `bok_core.findings.Finding`.

---

## 5. Errors — RFC 9457 `problem+json`

```json
{ "type": "https://selector-drift.dev/problems/still-invalid",
  "title": "Promotion refused: the gate still fails",
  "status": 409,
  "detail": "2 checks still fail after human confirmation.",
  "failing": [ { "check": "RowKeyContinuity", "outcome": "FAIL", "...": "..." } ] }
```

| Exception | Status | `type` slug |
|---|---|---|
| no / expired session | 401 | `unauthenticated` |
| `Origin` mismatch | 403 | `bad-origin` |
| unknown id / run_key | 404 | `not-found` |
| path or body validation | 422 | `invalid` |
| `StillInvalid` on promote | 409 | `still-invalid` (carries `failing`) |
| already resolved | 409 | `already-resolved` |
| login throttle | 429 | `throttled` |
| bundle manifest mismatch | 200 with `available=false, unavailable_reason="corrupted"` on the view; `410` on the file route | `evidence-corrupted` |
| bundle deleted under retention (§7.4) | 200 with `available=false, unavailable_reason="expired"` on the view; `410` on the file route | `evidence-expired` |

The `type` URIs are relative to the configured public base URL and resolve to anchors in
`docs/api.md`, so a reader of the error lands where the problem type is documented. (The domain in
the example above is illustrative; the real base URL is set when P-2 names the account.)

---

## 6. `derive.py` — every computed figure, once (plan Task 34B)

### 6.1 Functions

```python
def run_success_rate(db, target: str | None, window: tuple[date, date]) -> float
def rung_shares(db, target: str | None, window) -> dict[str, float]      # denominator: every resolution; {} if none
def missed_runs(db, target: str | None, window) -> int                   # expected cron slots with no heartbeat
def fired_without_attempt(db, target: str | None, window) -> int         # heartbeat present, no run_attempts row
def elapsed_days(db, target: str) -> int                                 # since the first heartbeat
def heal_summary(heal: HealEvent) -> str                                 # the §5.4 sentence
def stats(db, target: str | None, window) -> StatsView
```

`run_success_rate` reads `run_attempts`, never `runs` — the whole reason `run_attempts` exists (audit
M13). `rung_shares`' denominator is every row in `resolutions`, including rung-1 hits (audit C4).

**A missed run needs a table the frozen schema did not have.** Plan Task 6's `heartbeat()` *"writes a
row every scheduled fire"* — into no table named anywhere in Task 3. And a heartbeat alone cannot
record a fire that never happened: the host that was down writes nothing. So:

- **`heartbeats(id, target, scheduled_for, fired_at, run_key NULL)`** joins the day-one schema
  (Task 3), indexed on `(target, scheduled_for)`. Unlike the auth tables, this one **is** log data:
  it is the denominator of the published missed-run count, and missing on day one means missing for
  ninety days.
- `missed_runs` = the slots the target's cron (`spec.schedule`, as versioned at the time) expected in
  the window, minus the slots with a heartbeat. A down host shows up as missed, which is the honest
  reading (design §9.2).
- `fired_without_attempt` = heartbeats with no `run_attempts` row — the scheduler fired and the run
  died before it began (a refused scope, a crashed worker). Published separately, because the two
  failures have different causes.

### 6.2 Why aggregates are not `Claim`s yet

`bok_core.provenance.Source` has seven variants (`INTERFACE-CONTRACT.md` §2.2) and none of them is
"a query over this product's own tables". The contract's rule is that a symbol not in the document
does not exist. So `StatsView` returns plain numbers, each paired with a `method_url` into
`docs/methodology.md`, where the exact query is written out, and the UI renders them as figures with
a "how this is computed" link rather than as `<Claim>`s.

**Proposed contract amendment, flagged for sign-off rather than applied:**
`QuerySource(query_id: str, sql_sha256: str, as_of: datetime)`. If it is accepted, `StatsView`'s
fields become `Claim[float]` with `method=DERIVED` and the UI change is one component swap. Plan
Task 34B carries the proposal text.

---

## 7. The evidence bundle (amends plan Tasks 19, 26A and 33)

### 7.1 The problem it solves

The heal-log screen is before/after. "Before" is the page as it was on the **last accepted run**,
the run whose selector later broke, recorded as `selector_cache.set_by_run_key`. In the first
revision that page was never stored, because evidence was written only when a heal happened. The
screen had nothing to put on its left side.

### 7.2 Two stores, no schema change

```
evidence/
  last-accepted/<target>/        overwritten on every ACCEPTED run of that target
    page.html  page.png  manifest.json
  heals/<heal_id>/               written once, at heal time; heal_events.evidence_uri points here
    before.html  before.png      copied from last-accepted/<target>/ at heal time
    after.html   after.png       this run's capture
    manifest.json
```

```json
{ "version": 1,
  "files": { "before.html": "sha256:…", "before.png": "sha256:…",
             "after.html":  "sha256:…", "after.png":  "sha256:…" },
  "before": { "run_key": "…", "captured_at": "2026-01-01T04:12:00Z", "viewport": [1280, 900],
              "nodes": { "claim_number": { "node_id": "…", "bbox": {"x": 0, "y": 0, "width": 0, "height": 0} } } },
  "after":  { "run_key": "…", "captured_at": "…", "viewport": [1280, 900], "nodes": { } },
  "redaction": { "scrubbed_credentials": 3, "redacted_identifiers": 0, "verified": true } }
```

Rules:

- **Last-accepted moves only on acceptance.** A run that writes rows replaces
  `last-accepted/<target>/`; a quarantined run never does. This is the audit's C2 ordering rule — a
  rejected heal must not reach the selector cache — applied to evidence for the same reason. Written
  at the same call site as `cache.put()` in Task 26A, after `land.write()` returns.
- **Boxes come from the live page, at capture time.** `LivePageSource` (Task 13) records each
  resolved node's `getBoundingClientRect()` against a fixed viewport of 1280×900 before the page
  closes. `StaticPageSource` has no layout engine, so a box is never recomputed on replay; the heal
  screen outlines what was recorded and says so.
- **One write path.** Both stores go through Task 33's `store()`: capture → scrub credentials →
  redact → verify → `egress.guard_write` → write. A redaction failure quarantines the run with
  reason `evidence redaction failed` and writes nothing, including no update to `last-accepted/`.
- **Writes are atomic.** A bundle is written to `heals/.tmp-<ulid>/` and renamed into place, so a
  reader never sees a half-written bundle.
- **First heal of a target has no "before".** If `last-accepted/<target>/` is empty, the bundle's
  `before` block is absent and the view reports `unavailable_reason="not_captured"`.
- **A corrupted "before" is recorded, not copied.** If `last-accepted/<target>/` fails its own hash
  check at heal time, the bundle is written without `before.*` and the manifest carries
  `"before_unavailable_reason": "corrupted"`; the view reports that reason. A heal is never blocked by
  a fault in the evidence of the run before it.
- **Screenshots are masked at capture.** Credential inputs and every node the PII masker flags are
  masked in the screenshot before it leaves the browser, and `capture()` fails closed when an
  identifier sits in an element it cannot mask. Redaction of the DOM text alone cannot reach pixels.

### 7.3 Read path (`api/evidence.py`)

On every bundle read the manifest's SHA-256s are recomputed. A mismatch yields
`available=false, unavailable_reason="corrupted"` on `HealView` and `410` on the file route — the
file is **not** served. `replay_status` is `corrupted` when the file replay reads (`after.html`) fails
its hash. A heal whose bundle was deleted under retention reads `expired`. The UI renders all three
through `PartialFailureState` (see the web UI spec §5).

### 7.4 Retention (closes audit M30)

| Store | `self-owned` target | `client-authorised` target |
|---|---|---|
| `last-accepted/` | overwritten in place; one copy per target | same |
| `heals/` | kept for the life of the log | deleted after `evidence_retention_days` (scope record, **required** for this basis, default offered 90); the `heal_events` row and its counts remain |

`evidence_retention_days: int | None` joins `ScopeRecord` (plan Task 2) and the `scope_records`
mirror (Task 3). It is `None` for `self-owned` and **required** for `client-authorised`: a missing
value there is a refusal at `authorise()`, not a default, because an unbounded retention of client
screenshots is the outcome `00` B7 exists to prevent. A daily Prefect deployment `evidence_retention`
deletes expired bundles and writes an `audit_log` row per deletion. `docs/methodology.md` gets a
Retention section stating both policies and the VM's disk budget.

### 7.5 Tests (added to Tasks 19, 26A, 33, 34)

- `test_a_quarantined_run_does_not_replace_last_accepted`
- `test_heal_bundle_before_is_the_last_accepted_page`
- `test_first_heal_of_a_target_reports_before_not_captured`
- `test_boxes_are_recorded_from_the_live_page_not_recomputed_on_replay`
- `test_a_redaction_failure_leaves_last_accepted_untouched`
- `test_a_tampered_bundle_is_reported_corrupted_and_not_served`
- `test_client_target_without_retention_days_is_refused`
- `test_retention_deletes_client_bundles_and_keeps_counts`

---

## 8. TypeScript types and the drift check

`scripts/gen-types.sh` exports `app.openapi()` to `web/lib/openapi.json` and runs
`openapi-typescript` into `web/lib/api.d.ts`. Both files are committed. CI job `api-types` re-runs the
script and fails on any diff, so a Python response-model change that the UI has not absorbed fails
the build rather than a page. The web client is `openapi-fetch` typed by that file; no hand-written
response type exists in `web/`.

---

## 9. Test plan (plan Task 34)

Existing tests kept, with these corrections:

- **`test_discard_is_recorded_with_its_actor` was wrong in one way.** It sent `"actor": "me"` in
  the body, which the test beside it says must be `422`. Corrected: body `{reason}` only;
  asserts `state == "quarantined"` (the frozen enum has no `discarded`, which was correct),
  `quarantine_resolution == "discarded"`, and the audit row's actor is the session operator.
- `test_get_endpoints_return_200` uses ids from the seeded fixture (web UI spec §6), not literal `1`.

Added:

- `test_login_sets_an_httponly_strict_cookie_and_stores_only_its_hash`
- `test_a_non_get_with_a_foreign_origin_is_refused`
- `test_six_failed_logins_throttle_and_survive_a_restart`
- `test_no_request_model_declares_actor` — walks every request model in `views.py`
- `test_every_verdict_in_every_response_carries_a_threshold_field` — schema walk over the OpenAPI document
- `test_promote_that_still_fails_returns_409_with_the_failing_verdicts`
- `test_dom_evidence_is_served_sandboxed_and_nosniff`
- `test_stats_and_the_published_log_agree` — `derive.stats()` vs `heallog.publish` on the same fixture
- `test_api_imports_nothing_that_can_start_a_run` — the `api-is-read-side` contract
- `test_generated_types_are_current` — the CI drift check, runnable locally

---

## 10. Addendum 2026-10-19 — pre-flight against the code as built

Written before Task 34 starts, after Tasks 1–33 and 26A–C landed. The rest of this document stands.

1. **`StatsView` gains `false_accepts: int | None`** (amends §4.3). A wrong heal the gate let
   through is the figure the published log most needs, and the bench scorer already records it:
   `audit_log` rows with `actor="testbench.score"`, `action="scored"`, `detail.judgement ==
   "false_accept"`. `derive.py` counts them by actor and date window, exactly as it counts
   `quarantines_refused_correctly` (`judgement == "correct_refusal"`), and never parses `subject`.
   `None` for targets without an answer key. It is published beside `quarantines_refused_correctly`
   from day one; the answer key still never enters `selector_drift/`.
2. **Dependencies (Task 34 step 1).** `argon2-cffi` is added. `uvicorn` moves from the `testbench`
   dependency group into the main dependencies, because `serve-api` is a product entrypoint.
3. **Test environment.** `tests/api` truncates `operators`, `sessions` and `audit_log`, so it needs
   the Postgres the other DB tests use. The main checkout has no `.venv`; commands run as
   `python -m uv run --frozen …` where `uv` is not on PATH. Bringing Postgres up is a prerequisite
   step of the Task 34 plan, not an assumption.
4. **Unchanged from §6.2.** The vendored bok-core has no `QuerySource`, so `StatsView` stays plain
   numbers with `method_url`; the proposal remains a document (Task 34B).
