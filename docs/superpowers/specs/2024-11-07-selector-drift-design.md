# Selector Drift — Design

**Status:** approved 2024-11-07 · **amended 2026-08-29** ([audit](../reviews/2026-08-29-plan-audit.md)) · **sub-specs added 2026-09-26** (§3.3) · **Phase:** build weeks 1–8 · **Capacity:** 35–40 h/week (~300 h)
**Depends on:** `../Starting_Documents/00-SHARED-FOUNDATION.md`, `../Starting_Documents/03-SELECTOR-DRIFT.md`
**Licence:** Apache-2.0 · **Repo visibility:** public at first release (build week 8) · **Accent:** drift indigo `oklch(0.55 0.14 268)`

> **Week numbering, decided 2026-08-29 (audit OQ1).** This project runs **out of programme order,
> first** — weeks 1–12 have not run. Week numbers here and in the plan are project-relative (the
> brief's "weeks 13–20" is build weeks 1–8) and **no calendar date is assumed**. The log's start date
> is whatever Task 6's first cron fire records.

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
| Q1 | Ninety-day log targets | `testbench` (treatment arm) + two self-owned deployed applications (control arm). All `self-owned`. An uncontrolled target may be added later with its own honest start date. **Amended 2026-08-29 (OQ1):** the original answer named the `agent-perimeter` and `ledger-sense` demos, which do not exist and are ~12 weeks away now that this project runs first. Plan precondition P-3 names two existing self-owned deployments instead; the control arm's requirement — targets we own, whose deploy history we can read — is unchanged. |
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
testbench/       the mutating portal + mutation engine  (shipped artifact; Python SSR — §7)
web/             Next.js 15 + bok-ui
```

**Sub-specs, added 2026-09-26.** Four areas this design left thin are specified in their own
documents, written contract-first so the UI, the published log and the fixtures share one shape:

| Sub-spec | Covers | Plan tasks |
|---|---|---|
| [API contract and evidence bundle](2026-09-26-selector-drift-api-contract.md) | routes, read models, auth and the audit actor, errors, `derive.py`, the evidence bundle and retention, generated TS types | 3, 19, 26A, 33, 34, 34B, 40A |
| [Web UI](2026-09-26-selector-drift-web-ui.md) | Next.js architecture, every screen, designed states, `00` §5 cross-cutting requirements, E2E fixtures | 35–39A |
| [Test-bench portal](2026-09-26-selector-drift-testbench-portal.md) | server-rendered portal, request path, synthetic dataset, served-equals-tested test, deployment | 7, 8, 12 |
| [`bok-ui` slice](2026-09-26-selector-drift-bok-ui-slice.md) | the minimal `bok-ui` built in `backoffice-kit`, component contracts, the contract change it implies | 34A |

Where a sub-spec and this document disagree, the sub-spec is newer and wins; each disagreement is
also corrected here.

**Departure from the brief's tree:** `validate.py` and `quarantine.py` move out of `resolve/` into their own `validate/` package. A module that resolves selectors and also judges its own resolutions is one refactor away from a shortcut, and §5.6's independence argument requires the separation to be real.

### 3.4 Import contracts — enforced in CI by `import-linter`

| Contract | Why |
|---|---|
| `resolve/` must not import `land/` | A resolver cannot write. |
| `resolve/` must not import `validate/` | The judged cannot call the judge. |
| `validate/` may reach rungs only via the public `Rung` protocol | §5.6's independence must be structural. |
| `heallog/` must not import `browser/` | Replay must use stored evidence, not re-fetch. |
| `selector_drift/*` must not import `testbench/answers/*` | The answer key is unreachable from the resolver. **Without this the ninety-day log is worthless as evidence.** Widened 2026-09-26 to all of `testbench.*` ([bench spec](2026-09-26-selector-drift-testbench-portal.md) §2). |
| `selector_drift/*` may import from `bok_core` **only** the allow-listed submodules | **Amended 2026-09-26 from the Task 1 ledger.** The original contract forbade `bok_core.gateway` by name, which import-linter cannot express: with `include_external_packages` it squashes an external package to its top-level node and cannot tell `bok_core.gateway` from `bok_core.provenance`. For the same reason an `ignore_imports` allow-list cannot be relied on. The replacement is an AST test (plan Task 1, `test_bok_core_allowlist.py`) that fails on any `bok_core` import outside an explicit allow-list — `boundary.sensitivity` and `provenance` now, `boundary.redact`, `boundary.egress` and `findings` when Tasks 29 and 33 need them. Strictly stronger: it rejects `gateway` and anything else not named. |
| `api/` must not import `browser/`, `land/`, `orchestrate/`, or any of `resolve/` except the plain types in `resolve/trace.py` | The API reports on runs and never starts one; promotion reaches `orchestrate/` through an injected `QuarantineActions` ([API spec](2026-09-26-selector-drift-api-contract.md) §2). |
| only `orchestrate/cache_writer.py` writes the selector cache; `resolve/`, `validate/`, `api/` and `heallog/` may not import it | **Added 2026-09-26.** R11/C2's `SelectorCache.put(validated, …)` lived in `resolve/cache.py`, which then had to import `validate/` — breaking "the judged cannot call the judge". The write moved to `orchestrate/`, the one package allowed to import both; `resolve/` keeps the read side. C2's guarantee is unchanged: the write still cannot be spelled without a `ValidatedExtraction`. |

### 3.5 What this consumes from `bok-core` and `bok-ui`

`boundary/` (Sensitivity, redaction + its verification pass, `egress.guard_write`), `provenance/` (`Claim`, `DomSource`), `obs/`, `findings/` (severity ladder + SARIF for quality violations).

**It does not consume `gateway/`.** No lanes, no probe, no quota governor, no response cache — v1 makes zero model calls. This project can be built end to end even if `bok-core`'s gateway is unfinished or a provider vanishes mid-phase.

**How it is consumed, until the packages publish (Task 1 ledger, 2026-09-26).** Neither `bok-core` nor
`bok-ui` is on PyPI or npm. Both are hand-built as minimal slices in `backoffice-kit`
(`bok-core-minimal`, `bok-ui-minimal`) and consumed as **absolute-path** dependencies (`uv add
--editable`; `"file:"` in `web/package.json`). A `git+file://` dependency was tried first and hit a
reproducible `uv` panic; a relative path breaks on worktree merge. The exit is one line per package
when they publish. The `bok-ui` slice is specified in its [own document](2026-09-26-selector-drift-bok-ui-slice.md).

Every extracted value is a `Claim` with `method=DETERMINISTIC` and therefore `confidence=None` — the honest encoding (`00` B10). **No confidence element is rendered anywhere**, not even a greyed-out `ConfidenceMeter`: there is no score in this product (§5.4), and a meter reading "uncalibrated" would imply one exists. Superseded 2026-09-26; the earlier text said `ConfidenceMeter` would render as uncalibrated.

---

## 4. The scope gate

### 4.1 `scope.yaml`

```yaml
target: testbench
basis: self-owned                # enum, not free text
authorising_party: <name>
authorised_on: <the day the record is signed>   # never a future date (audit B4)
expires_on: <authorised_on + 1 year>
evidence_uri: null               # REQUIRED when basis == client-authorised
evidence_sha256: null            # REQUIRED with evidence_uri; re-verified at authorise time (audit H10)
origins: [https://bench.example] # the browser's allowlist (audit H2)
rate_limit: { requests: 1, per_seconds: 30 }
user_agent_contact: https://...
first_heal_policy: auto_promote  # required | auto_promote   (see 5.8)
warmup_runs: 5                   # bench 5, client targets 30 (5.7)
evidence_retention_days: null    # null only for self-owned; REQUIRED for client-authorised (API spec 7.4)
legal_review: self_drafted       # none | self_drafted | reviewed_by:<firm>
# no `sensitivity:` key — it is DERIVED from basis and cannot be authored (audit R4/H7)
```

*Corrected 2026-09-26: this example still carried an author-writable `sensitivity: SYNTHETIC` line,
which the 2026-08-29 revision removed from `ScopeRecord` — one typo in that field would publish a
client portal. It also lacked `origins`, `evidence_sha256` and `warmup_runs`, which the revision
added, and `evidence_retention_days`, which the API spec adds.*

### 4.2 Two authorisation bases in v1, not three

`self-owned` and `client-authorised`. **`public-permissive` is dropped from v1.**

It is the only basis whose defensibility rests on our own reading of contested law (B1: terms of service, computer-misuse statutes, database rights, the EU TDM opt-out regime, and US public-data case law that is narrower than the internet's summary of it). Given Q7's self-drafted-terms posture, it carries all the risk and buys nothing: all three log targets are self-owned and the sales posture is client-owned authenticated access. `client-authorised` requires an `evidence_uri` pointing at a countersigned document — written authorisation from a party who does have counsel.

Never circumvent authentication or defeat anti-bot measures. A target requiring evasion is out of scope, and the proposal says so (B6).

### 4.3 Fail-closed behaviour

Unknown target → refuse. Expired authorisation → refuse. Basis not in the v1 enum → refuse. Rate limit is read **from the `AuthorisedTarget`, never from config** — raising it means editing the scope record, which is the document you would point at if anyone ever asked why you accessed a system. It is not a tunable.

---

## 5. The resolution cascade and the validation gate

### 5.1 The rungs

| Rung | Method | Family | Defeated by | Survives |
|---|---|---|---|---|
| 1 `cache` | Stored selector **+ stored anchor evidence**, both re-verified; must match exactly one **visible** element and satisfy assertions | — | — | nothing changing |
| 2 `axtree` | ARIA role + accessible name + relationship | text-semantic | label rewording (**fails**, does not mis-resolve) | restyling, DOM restructure, class churn, id churn |
| 3 `anchors` | Text anchor + structural relation | text-semantic | label rewording (**fails**); layout inversion (**mis-resolves**) | class renames, id churn, framework swap, table→div |
| 4 `heuristics` | `data-testid`, id patterns, header position, column index **with header verification** | structural | label rewording together with a class rename on the same day (**fails**; see the note below) | label rewording alone, restyling, **column reorder** — it re-reads *today's* header, so a moved column is followed, not mis-resolved |
| 5 `visual` | v1: records the case with full evidence, raises `EscalateToHuman` | — | — | — |

**"Defeated by" now distinguishes *fails* from *mis-resolves*, and the distinction is load-bearing**
(audit L6, C3). On a label rewording rungs 2 and 3 return `None` — they do not point somewhere wrong.
That matters because §5.6's confirmer table assumed they would still resolve; they do not, so the
text-semantic family is *absent* rather than *wrong*, and with only two families the gate has no
confirmer left. See §5.6a.

Rung 1's "**exactly one**" is load-bearing. A selector matching two elements after a change is a
failure, not a first match.

**Column reorder, as built (corrected 2026-09-26).** An earlier text of this table had rung 4
*mis-resolving* on a column reorder. It does not: header verification reads the header row as it is
today, so rung 4 follows a moved column. The reorder hazard is **rung 1's** — a cached positional
selector still matches one plausible cell — and the protection is rung 1's anchor re-verification
(§5.2), which fails the hit so a heal is recorded. A rung-2 heal after a reorder is then confirmed by
rung 4 (`STRUCTURAL`). On the bench rung 4 finds a column by its `col-<field>` class or its header
position (the bench carries no `data-testid`), so a class rename and a label rewording on the same
day leave it nothing to read for that field: it escalates, and `docs/methodology.md` states this as a
known limit.

**Every rung filters candidates through a visibility precondition** — non-zero box, no `aria-hidden`,
no `display:none` ancestor (audit H9). Without it a hidden element carrying `aria-label="Claim
Number"` — a stale template, a print-only block, a screen-reader helper, or a decoy — resolves ahead
of the real one at rung 2, and every value it yields can satisfy the format assertion.

**Rungs take a `PageSource`, not a live browser handle** (audit C6/P1). Two implementations: live
Playwright, and static parsed DOM with a computed role/accessible-name mapping. This is what makes
§6's replay legal under §3.4's import contracts, lets the mutation suite run without a browser, and
turns hundreds of CDP round-trips per run into one snapshot.

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
7. **`RowKeyContinuity`** — **added 2026-08-29 (audit C3).** Joins this run's rows to the previous accepted run's on the spec's `natural_key`; for keys present in both, requires the healed field's values to agree at or above a stated rate. This is the second half of brief §5's *"do fields co-vary as they did historically?"*, which check 3 above implemented only as intra-row footing. See §5.6a.

### 5.6 `IndependentRungAgreement` — the check that does the real work

**The validator re-resolves the element using a rung from a different signal family than the one that healed it, and requires the two to point at the same node.**

Family matters, not rung number. Rungs 2 and 3 both key off text, so a label rewording can fool both identically and their agreement proves nothing. Confirmation must cross families:

| Healed by | Valid confirmer |
|---|---|
| 2 or 3 (text-semantic) | 4 (structural) |
| 4 (structural) | 2 or 3 (text-semantic) |
| 1 | n/a — rung 1 is never a valid confirmer; it is what broke |

This is the direct answer to §5's "most common and most dangerous silent failure," and unlike the distributional check it **needs no history and works on run one.** A decoy column that lands a text rung on the wrong cell is contradicted by rung 4's header-verified position, and vice versa. (A column reorder is *not* such a case: rung 4 follows the moved header, and the reorder's danger is rung 1's stale positional selector, which §5.2's anchor re-verification refuses — corrected 2026-09-26.)

If no cross-family confirmer can resolve at all, the verdict is `INSUFFICIENT`, never `PASS` — which under §5.7 quarantines the heal. Conservative and correct: that *is* the adjacent-column scenario.

The §3.4 import contract is what keeps this from decaying into the resolver marking its own work.
Agreement is compared on **node identity**, never on selector text (audit C5): `#claim` and
`td:nth-child(1)` routinely name the same node, and the same selector string can be produced against
different snapshots. The `PageSource` mints a `node_id`; the check compares those.

### 5.6a `Family.VALUE` — amendment of 2026-08-29 (audit C3). **Signed off 2026-08-29 (OQ3).**

**With two families, a plain label rewording quarantines every time, forever.** Rungs 2 and 3 both
*fail* on a reword (§5.1), rung 4 heals structurally, the confirmer must be text-semantic, and there
is none — so `INSUFFICIENT`, which §5.7 turns into `FAIL` on a heal. Label rewording is the most
common real UI change there is. On the bench, mutations re-seed daily, so it recurs with fresh
strings; `first_heal_policy: auto_promote` does **not** rescue it, because that policy satisfies only
check 6 while check 4 still fails. §14's mitigation for "log stalls on quarantine backlog" therefore
does not work, and the ninety-day log backs up in week 14.

The fix is a third signal family, and the brief already asked for it:

| Family | Signal | Independent of |
|---|---|---|
| `TEXT_SEMANTIC` | accessible name, label text | — |
| `STRUCTURAL` | `data-testid`, id pattern, header-verified column position | text |
| **`VALUE`** | row-key continuity against the previous accepted run | **both** — it reads the data, not the page |

| Mutation | Text | Structural | `VALUE` | Outcome |
|---|---|---|---|---|
| label reword | fails | heals | values unchanged, **confirms** | accepted |
| column reorder | heals | **confirms** — rung 4 re-reads today's header and lands on the same node | values unchanged | accepted (rung 1's anchor re-verification is what forced the heal) |
| decoy adjacent column | → Amount | → Prior Amount | values differ, **refuses** | quarantined |
| value swap between same-format columns | agrees | agrees | **refuses — the only check that sees it** | quarantined |

`VALUE` is a *stronger* discriminator for the adjacent-column case than DOM agreement, because it
looks at the data rather than at the page — and the last row is a case nothing else catches.

**The conservative alternative was considered and rejected (OQ3):** keep two families and state the
behaviour honestly — *"a label change costs one human confirmation, once, and never again."* That is
defensible on a client portal that reskins twice a year. It is not defensible on the bench, which
re-seeds its mutations daily — the same reword returns with a fresh string, so the confirmation is
nightly rather than once and the log stalls exactly as §14 says it must not. `VALUE` is also the only
family that sees a value swap between two same-format columns.

### 5.7 The asymmetry — how the gate avoids crying wolf

Strictness depends on whether anything healed:

| Winning rung | `INSUFFICIENT` treated as | Outcome |
|---|---|---|
| 1 (cache hit, anchor verified) | tolerated | write |
| 2–4 (healed) | **`FAIL`** | quarantine unless every check passes |
| 5 (escalated) | — | always quarantine |

This single decision makes B7 tractable. The distributional check — the one that *will* cry wolf on month-end, seasonality and genuine business change — **only gates a write when the selector actually changed.** A steady-state run with a cache hit and a genuinely shifted distribution raises a quality alert; it does not block. A heal into a shifted distribution blocks. The client never receives an alert they want to mute, because the blocking version fires only on the rare event where blocking is obviously right.

Warm-up falls out for free: with no history, `DistributionalAssertion` returns `INSUFFICIENT`, so **any heal during warm-up quarantines** — correct, because there is no basis to trust it yet.

**One steady-state `FAIL` exists, by decision (2026-09-26): the row-key collapse floor.** On a cache
hit, `RowKeyContinuity` still compares the field with the previous accepted run on the natural key:
at or above 0.95 agreement it is `INSUFFICIENT` (nothing healed, nothing to confirm); between 0.50
and 0.95 it is `INSUFFICIENT` **plus an alert** — ordinary churn, tolerated but reported; **below
0.50 (`COLLAPSE_FLOOR`) it is `FAIL`**, because no overnight business change moves half a column's
values while every key stays, and without it a value swap between two same-format columns under
unchanged markup would be written nightly on a rung-1 hit. On a heal, anything below 0.95 is `FAIL`
as before.

**Two corrections of 2026-08-29 (audit M17, M18).**

- **Persistence-across-runs governs alerting, not blocking.** Requiring two consecutive breaches
  before `FAIL` means a heal into an immediately shifted distribution passes on run one — the only run
  that matters, since the first day's bad data is written by then. On a heal, a **single-run breach is
  `FAIL`**; in steady state, persistence still applies and still solves B7.
- **Warm-up length lives on the scope record, not as a global 30.** Thirty daily runs is thirty days
  of quarantining every heal. The synthetic bench, which has a known answer key, warms up in 5; client
  targets keep 30. Like `first_heal_policy`, putting it on the scope record means it cannot be quietly
  lowered for a client target.

### 5.8 First-heal policy lives on the scope record

Default `required`. The bench sets `auto_promote` **because it is synthetic with a known answer key** — otherwise nightly runs against a mutating target back up in a quarantine queue and the ninety-day log stalls in week 14 while its operator is asleep.

Putting the policy on the scope record, next to the authorisation basis, means it cannot be quietly disabled for a client target. `docs/methodology.md` states which arm ran under which regime.

### 5.9 Quarantine

Keeps data, evidence and diff. One-click promote or discard, audited with actor, timestamp and reason. Promotion re-runs the gate. There is no code path turning a `QuarantinedRun` into a written row without `validate/` minting the result.

**What a human's confirmation supplies (stated 2026-09-26).** Promotion re-runs the gate with
`human_confirmed=True`, which **tolerates `INSUFFICIENT` on a heal — never a `FAIL`**: a person can
supply the evidence that was missing (a first heal, a warm-up, a confirmer that did not resolve), but
not overrule a check that found evidence against the heal. The promotion is recorded only after its
rows have landed, so a run is never marked promoted with nothing written.

Empty state reads *"Nothing quarantined"* with the count of runs that passed validation — not a celebration.

---

## 6. Evidence, redaction, and replay

**Write path: capture → redact → verify → store.** `bok-core`'s redaction re-scans its output and fails loudly if any original identifier survives. On failure the evidence is **discarded and the run is quarantined** with reason `evidence redaction failed`. Un-redacted evidence is never written to be cleaned up later; there is no later.

Stored per heal: redacted DOM snapshot, redacted-and-verified screenshot, full `CascadeTrace`, every `Verdict`, before/after selector, before/after anchor evidence, content hashes.

**Where "before" comes from — added 2026-09-26.** A heal's "before" is the page on the last
*accepted* run, and in the first revision that page was never stored. Every run now keeps a rolling,
redacted snapshot of its last accepted page per target; a heal copies it into a **heal bundle**
alongside this run's capture, with a manifest of SHA-256s and the bounding box of every resolved node,
recorded from the live page at capture time. A quarantined run never replaces the last-accepted
snapshot — the C2 ordering rule applied to evidence. Bundle layout, the read path that refuses to
serve a tampered file, and the retention policy that closes audit M30 are in the
[API spec](2026-09-26-selector-drift-api-contract.md) §7.

**Replay** re-runs the cascade against the *stored DOM* and asserts the same rung fires and the same element resolves. `heallog/` cannot import `browser/` (§3.4), so replay structurally uses stored evidence. A heal that fails to replay is **marked non-reproducible in the log rather than quietly counted** — B5 says an unreproducible heal is an anecdote, so the log says so.

**The reuse that pays for this: the mutation regression suite and the replay path are the same code.** A mutation test is "replay the cascade against this captured DOM, assert this rung and this verdict set." Each mutation class costs one fixture, not one harness.

---

## 7. The test bench

```
testbench/
  portal/        FastAPI + Jinja2, server-rendered: login - search - results - detail - export
  mutations/     one module per class, each a pure markup transform
  schedule.py    seed = f(date, target) -> the mutation set for a day
  answers/       ground truth for a seed   <- unreachable from selector_drift/ (3.4)
  score.py       judges each bench run; the only bench module that reads the product's tables
```

**The portal is server-rendered Python, not Next.js — decided 2026-09-26.** The audit's C7 fix routed
Next.js-rendered HTML through a Python mutator from `middleware.ts`. That cannot work: Next middleware
runs *before* rendering and cannot rewrite a page body, and even with the proxy reversed, Next's
client-side hydration re-renders the mutated page back to the unmutated one in the browser — the
very browser the resolver drives — while every HTTP-level test passes. The portal now renders with
Jinja2 and every HTML response passes through the same `apply()` chain in the same process: *served
equals tested* by construction, and the guarding test compares the DOM **in a browser**. Full
specification: [test-bench spec](2026-09-26-selector-drift-testbench-portal.md).

**Mutations are a runtime markup transform driven by a seeded, date-derived schedule** — not a build-time source rewrite committed to git. Deterministic and replayable from the seed alone; no git noise from ninety days of mutation commits; tests pin the seed and get the exact DOM the log saw; zero Actions minutes consumed, leaving quota for the nightly runs. Real deploy mechanics were never what is being tested — DOM change is.

**Mutation classes:** class renames · DOM restructuring · label rewording · column reordering · framework-level markup change · id churn · table→div conversion · pagination change.

**Adversarial class:** decoy adjacent column carrying plausible values. Every adversarial mutation gets a test asserting the gate **refuses**. Brief §11 says this set matters more than the heal set, and it is the set §5.6 exists for.

**Deployed as a container on the same always-on VM as the scheduler (§9.2), at a stable URL** — decided 2026-08-29 (OQ4). Vercel's Hobby tier is non-commercial and the bench is the artifact a paying buyer is handed; self-hosting on a machine that has to exist anyway costs nothing, removes a second free-tier quota from the nightly path (`00` B6), and lets the browser's origin allowlist name a single host. Mutation is a runtime transform driven by a date-derived seed, so the bench needs no platform feature beyond serving HTTP. Bundled in this repo; lifted out with its history later if it earns it.

---

## 8. The ninety-day log

**Task 1 of the plan. Day one, running whatever exists.** What must exist on day one:

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
| Control | two self-owned deployments (named in plan P-3) | **deploy history** | no spurious heals |

The control arm is the half nobody else has. A resolver that heals correctly and one that heals constantly look identical if every target is churning. Its claim is **"heals fired only on runs following an actual deploy, verified against deploy history"** — not "zero heals," because dependency bumps and platform changes will churn markup, and a heal there is the best evidence in the log precisely because it is a real change we did not author.

### 8.2 Publishability

All three day-one targets carry `SYNTHETIC`/`PUBLIC` data by construction, so the **published heal log can contain real screenshots with no redaction argument.** B4 dissolves. A consenting client portal added later is `CLIENT_CONFIDENTIAL` and contributes **counts only** — no screenshots, no DOM, ever.

**Where the publisher runs — corrected 2026-09-26.** The static export is built by a weekly Prefect
deployment **on the VM**, which is where Postgres and the evidence live, and pushed to the `gh-pages`
branch. The plan's first draft scheduled it as a GitHub Actions workflow, which cannot reach a
compose-only database on a private VM. Every figure it publishes comes from the same `derive.py` the
operator UI uses ([API spec](2026-09-26-selector-drift-api-contract.md) §6), so the public log and the
screen cannot disagree.

### 8.3 Headline shape

> *"90 days. 41 UI changes across the bench, zero manual selector fixes, 2 correct quarantines — and 90 days against two production apps where heals fired only on real deploys."*

`docs/methodology.md` states per-target start dates, which arm ran under which first-heal regime, what counts as a UI change, what counts as a successful heal, and what the sample is.

---

## 9. Storage, orchestration, and terminal states

### 9.1 Storage

**Postgres, compose-only, never Supabase** (§2.2 Q4): run history, heal events, selector cache, quarantine, scope records, audit log, and operator accounts and hashed sessions ([API spec](2026-09-26-selector-drift-api-contract.md) §3). Alembic migrations, least-privilege role, no superuser. Evidence bundles live on disk beside the lake, not in Postgres (API spec §7).

**Lake:** one Parquet file per run into a partitioned tree. DuckDB is the *query* layer over it, **never a shared file written by parallel flows.** A compaction flow folds small files on a schedule.

**Idempotency (B9):** deterministic run key `(target, spec_version, window)` plus dedup on the spec's
declared `natural_key`. **Files are append-only** — `{run_key}-{attempt}-{ulid}.parquet` — and the
`lake` view keeps the row with the greatest `ingested_at` per natural key. Late records land in their
correct partition and mark it dirty for recompaction; a closed partition is never mutated in place.
Compaction writes to a sibling directory and swaps atomically, so a concurrent reader never globs a
file that has just been unlinked. Tested by deliberately replaying a run.

*Amended 2026-08-29 (audit M6, M5, L7): "one Parquet file per run" and "never mutated in place" were
mutually exclusive — re-writing a run's file to make a replay idempotent is mutation in place — and
"the natural key" was named in three places while no schema anywhere defined one. It is now a required
field on `TaskSpec`.*

**Change-set export (§2.1 Q3):** reads from the lake, diffs against a client-supplied prior snapshot, emits per-row accept/reject in their import format. Thin by design — no connectors in v1.

### 9.2 Orchestration

Prefect 3, one deployment per `(target, spec)`, Postgres shared with the application — the same
instance, in a **separate database** (`prefect`), created by an init script on first start (plan
Task 6; corrected 2026-09-26 from "a separate schema"). A host restart loses no schedule either way;
a separate database keeps Prefect's migrations out of the application's.

Playwright per B8: pinned browser version, **one context per run**, hard memory and pid limits and a
timeout set at the deployment level, non-root, read-only rootfs, tmpfs scratch, **seccomp default —
never `unconfined`** (`00` §8), `cap_drop: [ALL]`, `no-new-privileges`, an explicit `shm_size`
(Chromium's Docker default of 64 MB is a known crash source) and `PLAYWRIGHT_BROWSERS_PATH` so a
non-root uid can reach browsers installed at build time. Aggressive reaping.

**The browser is confined to the authorised origins** by a route handler built from the
`AuthorisedTarget` (audit H2). The scope gate guarantees no page without a scope record — true of the
first navigation and of nothing after it; a redirect, an SSO hop or an absolute URL in a nav step
walks off the target while still inside an authorised context. The **rate limit** from the scope
record is enforced by a persistent token bucket, not a per-run one (audit H4): `1 req / 30 s` inside a
run says nothing about ten runs starting at once.

A scheduled runner survives host restarts via Prefect's own persistence — never a `while true` loop.
**Prefect persistence solves a restart, not an absent host.** Ninety consecutive daily runs need a
machine that is on at 04:12 every night.

**Decided 2026-08-29 (audit OQ2): a free always-on VM** — Oracle Cloud Always Free or Fly.io — which
also hosts the test bench (§7). Its disk budget and retention policy are named in
`docs/methodology.md`, and missed runs are published rather than hidden (audit B3). Three properties of
that choice bind the container work: the free Oracle shape is `arm64`, so the pinned Playwright image
must be built for it; the host clock is set to UTC, because 04:12 in an assumed timezone shifts the
entire log; and free tiers may reclaim instances they judge idle, which belongs in the methodology as a
known gap rather than arriving as a surprise in build week 6.

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

**Added 2026-09-26.** The architecture, every screen's designed states, the `00` §5 cross-cutting
requirements this section did not scope (provenance rail, `cmdk` palette, print, 375px, density), and
the E2E fixtures are in the [web UI spec](2026-09-26-selector-drift-web-ui.md). The components come
from a minimal `bok-ui` slice built in `backoffice-kit` ([`bok-ui` spec](2026-09-26-selector-drift-bok-ui-slice.md)),
because `bok-ui` does not exist yet now that this project runs first.

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
- **Adversarial suite** — every adversarial mutation is **refused** by the gate. *This set matters more.* **Seven classes**, not one: decoy adjacent column, hidden decoy with a matching accessible name, duplicate label, value swap between same-format columns, locale flip, virtualised rows, image header. One adversarial case tests one branch of one check.
- **Acceptance suite** — every *benign* mutation is **accepted** by the gate, with its confirmer named. **Added 2026-08-29 (audit C3):** without it a resolver that quarantines 100% of legitimate heals passes every other test here, and the headline *"zero manual selector fixes"* is only true because a human cleared the queue by hand each night. The refusal rate and the acceptance rate mean nothing apart — refusing everything scores 100% on the first and 0% on the second.
- **End-to-end pipeline** — a clean run writes; a healed run writes, records a heal and updates the cache; a quarantined run writes nothing **and does not touch the selector cache**. That last clause is the ordering the whole design rests on (audit C2): a rejected heal that reaches the cache becomes a rung-1 hit tomorrow, records no heal, and never engages §5.7 again.
- **Rate limit and origin allowlist** — the limit is read from the `AuthorisedTarget`, is persistent across processes, and a navigation off the authorised origin is aborted (audit H4, H2).
- **API authentication** — no route serves evidence or extracted rows to an anonymous caller, and `actor` is never accepted from a request body (audit H3).
- **Evidence carries no credentials** — no session cookie, CSRF token or password-field value survives into a stored snapshot (audit H6), and `CLIENT_CONFIDENTIAL` evidence cannot reach the demo plane, enforced by `bok_core.boundary.egress.guard_write` rather than a local guard.
- **Scope gate** — unknown target refused, expired authorisation refused, rate limit enforced, **basis outside the v1 enum refused** (§4.2).
- **Mint sites** — constructing `AuthorisedTarget` outside `scope/` raises; constructing `ValidatedExtraction` outside `validate/` raises.
- **Import contracts** — `import-linter` in CI, including answer-key isolation.
- **Rung 1 provenance** — a column reorder that leaves the cached selector matching exactly one valid-looking element **must fail rung 1** (§5.2). The single most important regression test in the repo.
- **Cross-family confirmation** — a same-family confirmer is rejected; no cross-family confirmer yields `INSUFFICIENT` (§5.6).
- **Credentials** — no plaintext at rest, nothing in logs, redaction verified on screenshots and DOM; **a test asserting no field named or typed for a TOTP seed exists anywhere in the schema.**
- **Quarantine** — a failing gate never writes; promotion re-runs the gate; promotion audited.
- **Idempotency** — replay a run, assert no duplicates; late record does not corrupt a closed partition.
- **Data quality** — synthetic shift detected; synthetic seasonality not falsely alerted after warm-up.
- **Degraded mode** — `test_degraded_mode_still_produces_findings` under `bok_core.testing.degraded.blackout()`, asserting this repo's floor and unit from `INTERFACE-CONTRACT.md` §5: *majority of mutation classes healed by rungs 1–4*. Trivially satisfied (no models), **kept as a regression guard** against a model dependency creeping in — and it must actually exist, which it did not before the 2026-08-29 revision (audit M3).
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
| Terms unreviewed (Q7) | scope gate narrowed to two bases; `client-authorised` requires countersigned evidence **whose SHA-256 is recorded and re-verified at authorise time** | §4.2 |
| **A label reword quarantines forever, stalling the log in build week 2** | third signal family, `RowKeyContinuity` (adopted, OQ3) | §5.6a |
| **A quarantined heal poisons the selector cache and goes silent permanently** | `cache.put()` takes a `ValidatedExtraction`; there is no way to spell the write that does not first spell "the gate passed" | plan Task 17, 26A |
| **The rung distribution, run-success rate and manual-fix count have no source** | `resolutions`, `run_attempts`, `interventions`, `deploys` — and, added 2026-09-26, `heartbeats`, the missed-run denominator ([API spec](2026-09-26-selector-drift-api-contract.md) §6.1) — ship in the day-one frozen schema | plan Task 3 |
| **A `sensitivity` typo publishes a client portal** | derived from `basis`, not authored; redaction unconditional; publication gated on an allowlist | §4.1, §6 |
| **The substrate does not exist yet** | day-one blocking surface narrowed to `boundary.sensitivity` + `provenance`; the rest is not needed until weeks 17–18 | plan preconditions |
| **No host is named for a ninety-day nightly cron** | free always-on VM (OQ2), `arm64` image, UTC clock; disk budget and retention in `docs/methodology.md`; missed runs published, not hidden | §9.2 |
| **The bench sits on a non-commercial free tier while being sold from** | bench self-hosted on the same VM (OQ4) | §7, §9.2 |
| **Hydration re-renders the mutated bench back to the original in the browser**, so the log scores heals against a page the resolver never saw (found 2026-09-26) | server-rendered Python portal, engine in-process; served-equals-tested asserted on the browser DOM | §7, bench spec §1, §6.1 |
| **The heal screen has no "before"** because the prior page was never stored | last-accepted snapshot per target, copied into a heal bundle | §6, API spec §7 |
| **UI components do not exist** — `bok-ui` is scheduled for programme weeks 3 and 13, and this project runs first | minimal `bok-ui` slice in `backoffice-kit`, props pinned to contract §6; contract edit drafted for sign-off | §3.5, `bok-ui` spec |
| **Aggregate figures cannot be `Claim`s** — no `Source` variant for a query over our own tables | plain figures with a method link; `QuerySource` amendment proposed, not assumed | API spec §6.2 |
