# Selector Drift — Design

**Status:** approved 2024-11-07 · **Phase:** weeks 13–20 · **Capacity:** 35–40 h/week (~300 h)
**Depends on:** `../Starting_Documents/00-SHARED-FOUNDATION.md`, `../Starting_Documents/03-SELECTOR-DRIFT.md`
**Licence:** Apache-2.0 · **Repo visibility:** public at first release (week 20) · **Accent:** drift indigo `oklch(0.55 0.14 268)`

Self-healing extraction for business systems with no API.

> *"Your integration breaks every time that portal ships a UI change. Mine re-resolves the element, proves in a log that it healed correctly, and alerts you before bad data reaches your system."*

---

## 1. The two facts this design exists to serve

**1. Self-healing's failure mode is worse than breaking.** A broken selector throws and someone fixes it. A wrongly-healed selector returns plausible, incorrect data into a client's system silently, for weeks. The post-heal validation gate is the product; the write-back to cache is trivial.

**2. Legal scope fails closed.** The refusal is built before the crawler. No target runs without a scope record.

Both are enforced structurally, not by discipline — see §3.

---

## 2. Decisions settled before this design

### 2.1 From brief §13

| # | Decision | Answer |
|---|---|---|
| Q1 | Ninety-day log targets | `testbench` (treatment arm) + `agent-perimeter` and `ledger-sense` demo deployments (control arm). All `self-owned`. An uncontrolled target may be added later with its own honest start date. |
| Q2 | Credential posture | Three tiers behind one protocol: `SessionInjection` (default offer), `ClientBroker` (demo this), `EncryptedStore` (last resort, written acknowledgement). **No code path stores a TOTP seed.** MFA is satisfied by asking the client's broker for a fresh code, or the target is out of scope in v1. |
| Q3 | Write-back | Never write into a client portal. v1 ships a **reviewable change-set export** — extracted rows plus a diff against a client-supplied prior snapshot, per-row accept/reject, in their import format. Their system applies it. We never hold write credentials. |
| Q4 | Orchestrator | **Prefect 3.** Named in the strongest demand evidence in the brief; fits the `docker compose up` constraint; its persistence satisfies B8. |
| Q5 | Test bench | **Bundled** in `testbench/`. The split is reversible (`git filter-repo`), the merge is not. |
| Q6 | Rung 5 | **Deferred to an instrumented escalation.** See §5.3. |
| Q7 | Legal review | Self-drafted terms; review only if a deal demands it. **Contained by narrowing the scope gate — see §4.2.** |

### 2.2 From `00` §12 — resolved, and carried to the other three projects

| # | Decision | Answer |
|---|---|---|
| Q2 | Unified `bok` CLI | **No `bok-cli`.** Four independent products. A command registry in `bok-core` inverts the dependency direction. This project's buyer entrypoint is the Prefect scheduler; the dev entrypoint is `python -m selector_drift`. No `[project.scripts]`, no Typer/Click. |
| Q3 | Provider inventory | **Does not gate this project.** With rung 5 deferred, v1 makes zero model calls. The checklist is in §11 for the other three. |
| Q4 | Supabase | **Rule, not a yes/no:** any plane that can hold `CLIENT_CONFIDENTIAL` is compose-only self-hosted; Supabase is permitted only for `SYNTHETIC`/`PUBLIC` planes. Here that means **zero Supabase** — the published heal log is a static export with no database. |
| Q5 | Public from commit one | **No — public at first release**, per the programme decision. Two arguments for flipping early were considered and both fail: a private repo's free Actions allowance comfortably covers the ~450 min/month these nightly runs need, and flipping a repo public preserves its full git history, so the ninety-day log's start date stays verifiable either way. |
| Q6 | Apache-2.0 vs AGPL | **Apache-2.0.** AGPL's hosted-defensibility only bites a competing SaaS; nobody SaaS-ifies a bespoke integration resolver — the value is the integration work, which is not in the repo. AGPL also suppresses adoption of a benchmark. The AGPL question is genuinely live only for `agent-perimeter`. |

---

## 3. Architecture: the type-gated spine

The chosen approach of three. A central policy engine and a declarative pipeline were both rejected for the same reason: they make the gate something you *invoke*, and §1 says discipline fails at 2am.

### 3.1 The chain

```
scope.yaml
    |  scope.gate.authorise()              <- ONLY mint site for AuthorisedTarget
    v
AuthorisedTarget          carries: basis, authorising party, expiry, rate limit,
    |                              first-heal policy, legal_review state
    |  browser.open(target, credentials)
    v
Session -> PageHandle
    |  cascade.resolve(page, spec.target)
    v
ResolvedElement  (+ CascadeTrace: winning rung, per-rung failure reasons, evidence)
    |  extract.read(resolved, spec.schema)
    v
CandidateExtraction        <- land/ has no overload accepting this type
    |  validate.gate(candidate, history, heal_context)
    v
  +------------------------+------------------------+
  v                                                 v
ValidatedExtraction                          QuarantinedRun
  |  land.write()                                   |  promote(actor, reason)
  v                                                 +--> re-runs validate.gate()
Parquet + DuckDB                                         with confirmation present
```

### 3.2 Two mint sites — the whole safety argument

- **`AuthorisedTarget`** is constructible only inside `scope/`, enforced by a module-private token its constructor requires (~10 lines) plus a test asserting external construction raises. `browser/` exposes no `goto(url: str)`. **There is no path to a page without a scope record.**
- **`ValidatedExtraction`** is constructible only inside `validate/`. `land.write()` accepts nothing else. **There is no path to a written row without the gate.**

Quarantine promotion re-runs the gate with the human confirmation supplied as an input. It provides missing evidence; it never skips a check.

`mypy --strict` is already mandatory (`00` §3.6), so the chain is verified at build time at no extra cost.

### 3.3 Module layout

```
selector_drift/
  spec/          task spec schema, versioning, validation
  scope/         ScopeRecord, authorisation gate, AuthorisedTarget   <- build first
  credentials/   CredentialProvider protocol + three implementations
  browser/       Playwright driver, containerised, session + MFA handling
  resolve/       rungs/, cache.py, trace.py
  validate/      assertions/, gate.py, quarantine.py
  extract/       typed extraction into Pydantic schemas
  quality/       schema drift, null rates, row counts, distribution shift
  land/          Parquet writer, DuckDB catalog, idempotent upsert, changeset export
  orchestrate/   Prefect 3 flows, scheduling, retries, quota accounting
  heallog/       heal events, evidence storage, redaction, diffing, replay
  api/           FastAPI
testbench/       the mutating portal + mutation engine  (shipped artifact)
web/             Next.js 15 + bok-ui
```

**Departure from the brief's tree:** `validate.py` and `quarantine.py` move out of `resolve/` into their own `validate/` package. A module that resolves selectors and also judges its own resolutions is one refactor away from a shortcut, and §5.6's independence argument requires the separation to be real.

### 3.4 Import contracts — enforced in CI by `import-linter`

| Contract | Why |
|---|---|
| `resolve/` must not import `land/` | A resolver cannot write. |
| `resolve/` must not import `validate/` | The judged cannot call the judge. |
| `validate/` may reach rungs only via the public `Rung` protocol | §5.6's independence must be structural. |
| `heallog/` must not import `browser/` | Replay must use stored evidence, not re-fetch. |
| `selector_drift/*` must not import `testbench/answers/*` | The answer key is unreachable from the resolver. **Without this the ninety-day log is worthless as evidence.** |

### 3.5 What this consumes from `bok-core`

`boundary/` (Sensitivity, redaction + its verification pass), `provenance/` (`Claim`), `obs/`, `findings/` (severity ladder + SARIF for quality violations).

**It does not consume `gateway/`.** No lanes, no probe, no quota governor, no response cache — v1 makes zero model calls. This project can be built end to end even if `bok-core`'s gateway is unfinished or a provider vanishes mid-phase.

Every extracted value is a `Claim` with `method=DETERMINISTIC` and therefore `confidence=None` — the honest encoding, which makes `ConfidenceMeter` correctly render as uncalibrated (`00` B10).

---

## 4. The scope gate

### 4.1 `scope.yaml`

```yaml
target: testbench
basis: self-owned                # enum, not free text
authorising_party: <name>
authorised_on: 2026-09-07
expires_on: 2027-09-07
evidence_uri: null               # REQUIRED when basis == client-authorised
rate_limit: { requests: 1, per_seconds: 30 }
user_agent_contact: https://...
first_heal_policy: auto_promote  # required | auto_promote   (see 5.8)
legal_review: self_drafted       # none | self_drafted | reviewed_by:<firm>
sensitivity: SYNTHETIC           # SYNTHETIC | PUBLIC | CLIENT_CONFIDENTIAL
```

### 4.2 Two authorisation bases in v1, not three

`self-owned` and `client-authorised`. **`public-permissive` is dropped from v1.**

It is the only basis whose defensibility rests on our own reading of contested law (B1: terms of service, computer-misuse statutes, database rights, the EU TDM opt-out regime, and US public-data case law that is narrower than the internet's summary of it). Given Q7's self-drafted-terms posture, it carries all the risk and buys nothing: all three log targets are self-owned and the sales posture is client-owned authenticated access. `client-authorised` requires an `evidence_uri` pointing at a countersigned document — written authorisation from a party who does have counsel.

Never circumvent authentication or defeat anti-bot measures. A target requiring evasion is out of scope, and the proposal says so (B6).

### 4.3 Fail-closed behaviour

Unknown target → refuse. Expired authorisation → refuse. Basis not in the v1 enum → refuse. Rate limit is read **from the `AuthorisedTarget`, never from config** — raising it means editing the scope record, which is the document you would point at if anyone ever asked why you accessed a system. It is not a tunable.

---

## 5. The resolution cascade and the validation gate

### 5.1 The rungs

| Rung | Method | Family | Fooled by | Survives |
|---|---|---|---|---|
| 1 `cache` | Stored selector **+ stored anchor evidence**, both re-verified; must match exactly one element and satisfy assertions | — | — | nothing changing |
| 2 `axtree` | ARIA role + accessible name + relationship | text-semantic | label rewording | restyling, DOM restructure, class churn, id churn |
| 3 `anchors` | Text anchor + structural relation | text-semantic | label rewording, layout inversion | class renames, id churn, framework swap, table→div |
| 4 `heuristics` | `data-testid`, id patterns, header position, column index **with header verification** | structural | column reorder | label rewording, restyling |
| 5 `visual` | v1: records the case with full evidence, raises `EscalateToHuman` | — | — | — |

Rung 1's "**exactly one**" is load-bearing. A selector matching two elements after a change is a failure, not a first match.

### 5.2 Rung 1 verifies provenance, not just the selector

**The most dangerous hole in a naive version of this design.** Columns are reordered; the cached `td:nth-child(3)` still matches exactly one element; the value still satisfies `^[A-Z]{2}\d{8}$`. Rung 1 "succeeds," **no heal is recorded, and the strict path in §5.7 never engages.** That is the adjacent-column failure arriving through the one door left open.

Therefore the selector cache stores, alongside the selector, the **anchor evidence that justified it** at heal time — accessible name, label text, header text — and rung 1 re-verifies that anchor **on every run**. Anchor no longer holds → rung 1 **fails** → the cascade proceeds → a heal is recorded → the strict path engages.

The brief's tree says `cache.py — selector cache with provenance`. That provenance is load-bearing, not audit metadata.

### 5.3 Rung 5 is an escalation in v1

**Rung 5 is disabled by policy on every target that pays.** A client portal screenshot is `CLIENT_CONFIDENTIAL` (`00` R4, brief §3.6, B4) and can never reach a free tier. The only lawful route is a client-supplied paid key on a `trains_on_data: false` + `commercial_use: true` provider — a contract negotiation, not a v1 feature. On the exact $15–60k engagements this product exists for, rung 5 never fires. It could only ever fire on `SYNTHETIC`/`PUBLIC` targets, i.e. to make the demo prettier.

Three further reasons:
- B11 requires publishing the proportion reaching rung 5. **"0.0% of resolutions required a model call"** beats "2.7% did."
- `00` R3 sets a 70% deterministic floor. If rungs 1–4 cannot carry ~100% of the mutation suite, that is a resolver defect — and rung 5 becomes the excuse for a weak rung 3.
- Every escalated case is a labelled example of what rungs 1–4 could not do. That corpus is the eval set Ground Truth needs in weeks 21–26. Building rung 5 informed by it beats guessing at it in week 19.

v1 implementation: capture redacted evidence, record the case with the full cascade trace into the escalation corpus, raise `EscalateToHuman`, quarantine.

### 5.4 There is no confidence score — a deliberate departure from the brief

Brief §5 says *"heal confidence gates writes"* and the heal-log example reads *"confidence 0.94."* **Dropped.** Rungs 1–4 are deterministic; there is no model and therefore no score. Inventing one would be precisely the sin `00` B10 names — routing on an uncalibrated number while displaying it as if it meant something.

**The gate is a conjunction of verdicts, not a threshold on a scalar.** A heal-log line reads:

> Selector broke 04:12. Healed to `[aria-label="Claim Number"] + td` via **rung 3** (label anchor). Independently confirmed by **rung 4** (header-verified column position) — same element. Format `^[A-Z]{2}\d{8}$` ✓ · row footing ✓ · distribution unchanged (PSI 0.03, threshold 0.20, n=847).

Strictly more persuasive than `0.94`, and checkable — which is the programme's thesis.

### 5.5 The six checks

Each is an `Assertion` returning a **`Verdict`** — never a boolean. A `Verdict` carries outcome (`PASS` / `FAIL` / `INSUFFICIENT`), the threshold used, the observed value, and a reason in words. Brief §8 screen 5 forbids bare red badges; a `Verdict` that carries its threshold gives the UI nothing else it *could* render.

1. **`FormatAssertion`** — type/regex from the spec. Works on run one.
2. **`DomainAssertion`** — range, enum membership, date plausibility, currency sanity.
3. **`CrossFieldAssertion`** — does the row still foot, do fields co-vary as they did.
4. **`IndependentRungAgreement`** — see §5.6.
5. **`DistributionalAssertion`** — PSI (or KS), documented threshold, warm-up period, **persistence across runs required before alerting**, client can mark an alert expected (B7).
6. **`FirstHealConfirmation`** — first heal to a *new* selector completes into quarantine pending a human, subject to the scope record's `first_heal_policy`.

### 5.6 `IndependentRungAgreement` — the check that does the real work

**The validator re-resolves the element using a rung from a different signal family than the one that healed it, and requires the two to point at the same node.**

Family matters, not rung number. Rungs 2 and 3 both key off text, so a label rewording can fool both identically and their agreement proves nothing. Confirmation must cross families:

| Healed by | Valid confirmer |
|---|---|
| 2 or 3 (text-semantic) | 4 (structural) |
| 4 (structural) | 2 or 3 (text-semantic) |
| 1 | n/a — rung 1 is never a valid confirmer; it is what broke |

This is the direct answer to §5's "most common and most dangerous silent failure," and unlike the distributional check it **needs no history and works on run one.** A column reorder fools rung 4; rungs 2 and 3 disagree. A label rewording fools rung 3; rung 4's header-verified position disagrees.

If no cross-family confirmer can resolve at all, the verdict is `INSUFFICIENT`, never `PASS` — which under §5.7 quarantines the heal. Conservative and correct: that *is* the adjacent-column scenario.

The §3.4 import contract is what keeps this from decaying into the resolver marking its own work.

### 5.7 The asymmetry — how the gate avoids crying wolf

Strictness depends on whether anything healed:

| Winning rung | `INSUFFICIENT` treated as | Outcome |
|---|---|---|
| 1 (cache hit, anchor verified) | tolerated | write |
| 2–4 (healed) | **`FAIL`** | quarantine unless every check passes |
| 5 (escalated) | — | always quarantine |

This single decision makes B7 tractable. The distributional check — the one that *will* cry wolf on month-end, seasonality and genuine business change — **only gates a write when the selector actually changed.** A steady-state run with a cache hit and a genuinely shifted distribution raises a quality alert; it does not block. A heal into a shifted distribution blocks. The client never receives an alert they want to mute, because the blocking version fires only on the rare event where blocking is obviously right.

Warm-up falls out for free: with no history, `DistributionalAssertion` returns `INSUFFICIENT`, so **any heal during warm-up quarantines** — correct, because there is no basis to trust it yet.

### 5.8 First-heal policy lives on the scope record

Default `required`. The bench sets `auto_promote` **because it is synthetic with a known answer key** — otherwise nightly runs against a mutating target back up in a quarantine queue and the ninety-day log stalls in week 14 while its operator is asleep.

Putting the policy on the scope record, next to the authorisation basis, means it cannot be quietly disabled for a client target. `docs/methodology.md` states which arm ran under which regime.

### 5.9 Quarantine

Keeps data, evidence and diff. One-click promote or discard, audited with actor, timestamp and reason. Promotion re-runs the gate. There is no code path turning a `QuarantinedRun` into a written row without `validate/` minting the result.

Empty state reads *"Nothing quarantined"* with the count of runs that passed validation — not a celebration.

---

## 6. Evidence, redaction, and replay

**Write path: capture → redact → verify → store.** `bok-core`'s redaction re-scans its output and fails loudly if any original identifier survives. On failure the evidence is **discarded and the run is quarantined** with reason `evidence redaction failed`. Un-redacted evidence is never written to be cleaned up later; there is no later.

Stored per heal: redacted DOM snapshot, redacted-and-verified screenshot, full `CascadeTrace`, every `Verdict`, before/after selector, before/after anchor evidence, content hashes.

**Replay** re-runs the cascade against the *stored DOM* and asserts the same rung fires and the same element resolves. `heallog/` cannot import `browser/` (§3.4), so replay structurally uses stored evidence. A heal that fails to replay is **marked non-reproducible in the log rather than quietly counted** — B5 says an unreproducible heal is an anecdote, so the log says so.

**The reuse that pays for this: the mutation regression suite and the replay path are the same code.** A mutation test is "replay the cascade against this captured DOM, assert this rung and this verdict set." Each mutation class costs one fixture, not one harness.

---

## 7. The test bench

```
testbench/
  portal/        next.js: login - search - results table - detail - export
  mutations/     one module per class, each a pure markup transform
  schedule.py    seed = f(date, target) -> the mutation set for a day
  answers/       ground truth for a seed   <- unreachable from selector_drift/ (3.4)
```

**Mutations are a runtime markup transform driven by a seeded, date-derived schedule** — not a build-time source rewrite committed to git. Deterministic and replayable from the seed alone; no git noise from ninety days of mutation commits; tests pin the seed and get the exact DOM the log saw; zero Actions minutes consumed, leaving quota for the nightly runs. Real deploy mechanics were never what is being tested — DOM change is.

**Mutation classes:** class renames · DOM restructuring · label rewording · column reordering · framework-level markup change · id churn · table→div conversion · pagination change.

**Adversarial class:** decoy adjacent column carrying plausible values. Every adversarial mutation gets a test asserting the gate **refuses**. Brief §11 says this set matters more than the heal set, and it is the set §5.6 exists for.

Deployed from the subdirectory to Vercel free tier at a stable URL. Bundled in this repo; lifted out with its history later if it earns it.

---

## 8. The ninety-day log

**Task 1 of the plan. Day one of week 13, running whatever exists.** What must exist on day one:

1. Three `scope.yaml` records, `basis: self-owned`, gate refusing everything else.
2. A browser driver that opens an `AuthorisedTarget` and loads a page.
3. A task spec per target naming one or two fields.
4. A run record written to Postgres.
5. A Prefect deployment on a cron.

No cascade. Day one logs *run succeeded / run failed*. Heal events begin appearing around week 15 as the rungs land, and the start date stays honest because the run record was always there.

**The one place this design deliberately builds ahead of need:** the run and heal-event schema ships complete on day one, most columns null. You cannot compare day 1 to day 90 across a schema migration, and the comparison is the entire asset.

### 8.1 Two arms

| Arm | Targets | Ground truth | What it proves |
|---|---|---|---|
| Treatment | `testbench` | the seeded answer key | heals are correct, adversarial heals are refused |
| Control | `agent-perimeter`, `ledger-sense` demos | **deploy history** | no spurious heals |

The control arm is the half nobody else has. A resolver that heals correctly and one that heals constantly look identical if every target is churning. Its claim is **"heals fired only on runs following an actual deploy, verified against deploy history"** — not "zero heals," because dependency bumps and platform changes will churn markup, and a heal there is the best evidence in the log precisely because it is a real change we did not author.

### 8.2 Publishability

All three day-one targets carry `SYNTHETIC`/`PUBLIC` data by construction, so the **published heal log can contain real screenshots with no redaction argument.** B4 dissolves. A consenting client portal added later is `CLIENT_CONFIDENTIAL` and contributes **counts only** — no screenshots, no DOM, ever.

### 8.3 Headline shape

> *"90 days. 41 UI changes across the bench, zero manual selector fixes, 2 correct quarantines — and 90 days against two production apps where heals fired only on real deploys."*

`docs/methodology.md` states per-target start dates, which arm ran under which first-heal regime, what counts as a UI change, what counts as a successful heal, and what the sample is.

---

## 9. Storage, orchestration, and terminal states

### 9.1 Storage

**Postgres, compose-only, never Supabase** (§2.2 Q4): run history, heal events, selector cache, quarantine, scope records, audit log. Alembic migrations, least-privilege role, no superuser.

**Lake:** one Parquet file per run into a partitioned tree. DuckDB is the *query* layer over it, **never a shared file written by parallel flows.** A compaction flow folds small files on a schedule.

**Idempotency (B9):** deterministic run key `(target, spec_version, window)` plus upsert on the natural key. Late records land in their correct partition and mark it dirty for recompaction; a closed partition is never mutated in place. Tested by deliberately replaying a run.

**Change-set export (§2.1 Q3):** reads from the lake, diffs against a client-supplied prior snapshot, emits per-row accept/reject in their import format. Thin by design — no connectors in v1.

### 9.2 Orchestration

Prefect 3, one deployment per `(target, spec)`, Postgres shared with the application on a separate schema.

Playwright per B8: pinned browser version, **one context per run**, hard memory and pid limits and a timeout set at the deployment level, non-root, read-only rootfs, tmpfs scratch, seccomp default (`00` §8), aggressive reaping. A scheduled runner survives host restarts via Prefect's own persistence — never a `while true` loop.

### 9.3 Four terminal states, no fifth

| State | Trigger | Behaviour |
|---|---|---|
| **Refuse** | unknown target, expired authorisation, basis outside the v1 enum | never opens a browser |
| **Fail** | navigation, auth, timeout, crash | run marked failed, alert, no partial write |
| **Quarantine** | any `FAIL` verdict; any `INSUFFICIENT` on a heal; redaction verification failure | data + evidence + diff kept, promote/discard |
| **Escalate** | rung 5 | case recorded with evidence into the corpus, quarantined |

Everything fails closed. There is no "log a warning and continue."

### 9.4 Quality contracts

Row counts against expectation, null rates, schema drift, distribution shift — each emitted as a `bok-core` `Finding` on the shared severity ladder, reusing the SARIF emitter. Every alert carries its threshold and the reason for that threshold.

---

## 10. UI — scope call

Brief §8 specifies six screens. Estimated build for this design lands near 350 h against a ~300 h phase, and the overrun is concentrated here. v1 scope:

| Screen | v1 |
|---|---|
| 1 Targets — scope record surfaced prominently; unscoped targets render locked with the reason | **full** |
| 3 Run timeline — green completed / indigo healed / amber quarantined / red failed | **full** |
| 4 **Heal log** — the signature screen | **full** |
| 6 Quarantine — preview, reason, promote/discard | **full** |
| 2 Task spec editor | **read-only YAML + schema validation.** No Monaco, no form view, no live preview. |
| 5 Data quality | **table with thresholds and reasons stated.** No plotted distributions. |

Screen 2 is the expensive one and the least load-bearing: in a bespoke-integration v1, *we* write the task specs. An editor for non-engineers is speculative until a client asks to self-serve — which is also the trigger to build it.

**Screen 4 detail:** two-column before/after, screenshots left and right with the resolved element outlined, word-level DOM diff beneath, the winning rung, the cross-family confirmer, every `Verdict` with its threshold, and the promote/discard decision with actor and timestamp. The one orchestrated motion in the product: on open the outline draws itself around the element over 220 ms, first on "before" then on "after," so the eye tracks the move. Disabled under `prefers-reduced-motion`.

Light-first editorial instrument. Indigo for the heal state only. Severity and provenance state never encoded in colour alone.

---

## 11. Provider inventory — not needed for this project

With rung 5 deferred, v1 makes zero model calls, so `00` §12 Q3 does not gate weeks 13–20. It is still needed for the other three. Per provider, from the machine that will run the work:

| Check | How | Why |
|---|---|---|
| Reachable from your location | one real call, not a docs read | `00` B1 — geofencing is silent |
| Trains on inputs? | written terms, quoted verbatim, with URL and date read | `boundary/policy.py` `trains_on_data` |
| Commercial use permitted? | same, quoted | B3 — a licence breach counsel can find |
| Free tier includes vision? | live model list | rare; only matters if rung 5 revives |
| Card required? | signup flow | `00` §3.1 |

Record quote, URL and read-date in `docs/methodology.md`. The date is the point — every one of these is a snapshot.

---

## 12. Testing bar

Brief §11, plus what this design adds:

- **Mutation suite** — every mutation class heals correctly.
- **Adversarial suite** — every adversarial mutation is **refused** by the gate. *This set matters more.*
- **Scope gate** — unknown target refused, expired authorisation refused, rate limit enforced, **basis outside the v1 enum refused** (§4.2).
- **Mint sites** — constructing `AuthorisedTarget` outside `scope/` raises; constructing `ValidatedExtraction` outside `validate/` raises.
- **Import contracts** — `import-linter` in CI, including answer-key isolation.
- **Rung 1 provenance** — a column reorder that leaves the cached selector matching exactly one valid-looking element **must fail rung 1** (§5.2). The single most important regression test in the repo.
- **Cross-family confirmation** — a same-family confirmer is rejected; no cross-family confirmer yields `INSUFFICIENT` (§5.6).
- **Credentials** — no plaintext at rest, nothing in logs, redaction verified on screenshots and DOM; **a test asserting no field named or typed for a TOTP seed exists anywhere in the schema.**
- **Quarantine** — a failing gate never writes; promotion re-runs the gate; promotion audited.
- **Idempotency** — replay a run, assert no duplicates; late record does not corrupt a closed partition.
- **Data quality** — synthetic shift detected; synthetic seasonality not falsely alerted after warm-up.
- **Degraded mode** — trivially satisfied (no models), **kept as a regression guard** against a model dependency creeping in.
- **Replay** — a stored heal replays to the same rung and element; a corrupted evidence blob is reported non-reproducible, not silently counted.
- **E2E** — Playwright driving the UI, fixtures kept separate from the driver's own.
- **Accessibility** — axe, zero serious/critical.

Coverage floor 75%.

---

## 13. Definition of done — brief §12

1. Scope gate refuses unauthorised targets, proven by test.
2. **Restated:** Resolution cascade implemented with five registered rungs and rung-level instrumentation. Rung 5 escalates to human review in v1; the model implementation is deferred, the reason documented in `docs/methodology.md`, and escalated cases form a labelled corpus for Ground Truth (weeks 21–26).
3. Post-heal validation gate implemented; adversarial mutations refused, proven by test.
4. Quarantine with promote/discard and full audit.
5. Test bench shipped, open source, mutation engine documented.
6. Ninety-day heal log running since day one of the phase, with published methodology.
7. Typed landing zone with data-quality contracts and alerting, idempotent under replay.
8. Credentials handled per B2 with the chosen posture documented in `docs/security.md` — **three tiers, and no TOTP seed storage exists as a code path.**
9. Heal-log screen renders before/after with element outlines, DOM diff and assertion results.
10. `docs/methodology.md` states what the heal-log numbers mean: what counts as a UI change, what counts as a successful heal, what the sample is, per-target start dates, and which arm ran under which first-heal regime.

---

## 14. Known risks carried into the plan

| Risk | Mitigation | Where |
|---|---|---|
| Estimate lands ~350 h against ~300 h | UI scope call (§10); cut screens 2 and 5 to minimum | §10 |
| Ninety-day log stalls on quarantine backlog | `first_heal_policy: auto_promote` on the synthetic bench only | §5.8 |
| Control arm produces heals, weakening "zero heals" | claim restated against deploy history | §8.1 |
| Rung 1 silently returns the wrong element | anchor provenance re-verified every run | §5.2 |
| Same-family confirmation proves nothing | cross-family requirement | §5.6 |
| Distribution alerting cries wolf and gets muted | blocking only on heal; alerting otherwise | §5.7 |
| Playwright in containers | pinned version, one context, hard limits, reaping | §9.2 |
| Terms unreviewed (Q7) | scope gate narrowed to two bases; `client-authorised` requires countersigned evidence | §4.2 |
