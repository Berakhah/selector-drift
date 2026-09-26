# Selector Drift — spec and plan audit

**Date:** 2026-08-29 · **Auditor:** fresh session, no prior context
**Subjects:** `docs/superpowers/specs/2024-11-07-selector-drift-design.md` (452 ln), `docs/superpowers/plans/2024-11-07-selector-drift.md` (2730 ln, 41 tasks)
**Checked against:** `Starting_Documents/00-SHARED-FOUNDATION.md`, `Starting_Documents/03-SELECTOR-DRIFT.md`, `../backoffice-kit/docs/INTERFACE-CONTRACT.md` v0.1

---

## Method — what was actually verified, and how

Nothing below is inferred from the plan's own prose about itself. Every `VERIFIED` finding was
checked by running a command or reading a second document that the plan does not control.

| Check | Command | Result |
|---|---|---|
| Do the substrate and sibling repos contain code? | `find <repo>/docs -type f`, `git log --oneline -3` in all five repos | **No.** Each has exactly one commit, `docs: initial design specs and implementation plans`, and contains only `CLAUDE.md`, `Starting_Documents/`, `docs/`. Zero source files programme-wide. |
| Is the pipeline ever assembled? | `grep -n "flows" plan.md`, `grep -n "^- Modify:" plan.md` | `orchestrate/flows.py` is created once (Task 6) and appears in no later task's `Modify:` list. Six `Modify:` lines exist in the whole plan; none touch application code. |
| Are `00`'s cross-cutting UI requirements carried? | `grep -ic` for `ProvenanceRail`, `provenance rail`, `cmdk`, `375`, `Claim>` | 0, 0, 0, 0, 0 |
| Is degraded mode tested? | `grep -ic degraded plan.md` | 0 |
| Are the load-bearing deps declared? | `grep -ic` for `lxml`, `cryptography`, `pytest-asyncio` | 1 (prose only, not in `uv add`), 0, 0 |
| Is the rate limit enforced? | `grep -ic "rate limit" plan.md` | 1 (one prose mention; no step, no test) |
| Does the plan match the substrate's published API? | line-by-line against `INTERFACE-CONTRACT.md` §1–§7 | four mismatches, one omission of a *required* symbol |

Findings marked `ASSESSED` follow from reading the documents together; they are reproducible by any
reader but were not executed. Findings marked `VERIFY` are external facts (a vendor's terms, a
platform's behaviour) that this programme's own operating principle says to re-check on the day.

---

## Verdict

The **design** is better than most work of this kind. Four things in it are genuinely load-bearing and
correct: dropping `public-permissive` (§4.2), rung 1 re-verifying anchor provenance rather than the
selector (§5.2), refusing to invent a confidence score (§5.4), and the `INSUFFICIENT`-on-heal
asymmetry (§5.7). The two-mint-site spine is the right shape. Keep all of it.

The **plan** has three structural problems, in descending order of consequence:

1. **It is blocked on artifacts that do not exist** and does not say so. Day one cannot start.
2. **It builds every component and never assembles them.** There is no task that connects
   resolve → extract → gate → land → heallog. Forty-one tasks produce a parts bin.
3. **Several of its central safety claims are asserted in prose and enforced nowhere** — the
   rate limit, the cache write-back ordering, node identity in the one check the whole project
   rests on, and the *acceptance* half of the validation gate.

There is also one design-level defect that the plan inherits and amplifies: **as specified, a plain
label rewording quarantines every time, forever** (C3). That is not a bug in an edge case; it is the
most common real-world UI change there is, and it stalls the ninety-day log in week 14.

63 findings follow. 5 blockers, 7 critical, 10 high, 31 medium, 10 low.

---

## BLOCKERS — day one cannot start, or a headline claim cannot be made

### B1 · `bok-core` does not exist, and this project may not shim it · VERIFIED

`git log` in `../backoffice-kit` shows one docs-only commit; there is no `bok_core` package, no PyPI
release, and no `template/`. Plan Task 1 Step 1 runs `copier copy gh:USER/backoffice-kit .` and
Step 2 runs `uv add ... bok-core`; both fail. Task 2's `ScopeRecord.sensitivity: Sensitivity` imports
from it.

`INTERFACE-CONTRACT.md` §8 pre-authorises a `_bok_shim.py` fallback **but explicitly excludes
`boundary`**: *"A shim is never acceptable for `boundary`."* Selector Drift consumes `boundary` (§7).
So the documented escape hatch does not apply here.

**The blocking surface is small, and that is the way out.** Day one (Tasks 1–6) needs exactly
`boundary.sensitivity.Sensitivity` and `provenance.Claim` + `DomSource`. `redact`, `egress.guard_write`
and `findings` are not needed until Tasks 29/33 in weeks 17–18. Publish `bok-core 0.0.1` containing
only `boundary/sensitivity.py` and `provenance/` and day one is unblocked with weeks of slack on the
rest. Recorded as revision R1.

### B2 · The ninety-day log's control arm does not exist · VERIFIED

Design §2.1 Q1 answers "which three targets" with `testbench` plus `agent-perimeter` and
`ledger-sense` **demo deployments**, and §8.1 makes them the control arm — *"the half nobody else
has."* Both repos are docs-only. If Selector Drift starts before weeks 1–12 have run, the log has one
arm, §8.1's claim is undeliverable, and Task 5's `scopes/agent-perimeter.yaml` points at nothing.

This is not fixable in code. See Open Question 1.

### B3 · The scheduler has no host · VERIFIED (absence)

The primary sales asset is a ninety-day daily cron. The plan runs Prefect 3 inside
`docker compose`, and B8's mitigation is *"Prefect's own persistence, not a `while true` loop"* —
which solves *host restarts* and not *host availability*. No always-on machine is named in the design,
the plan, or `00`. A developer laptop misses every night it is closed, and a log with holes is worth
less than no log because the holes are visible in the artifact you are selling.

The plan does reason about GitHub Actions minutes ("~450 min/month", design §2.2 Q5) which implies
Actions as the runner — but Actions is never wired to the flow, and `schedule:` triggers are
best-effort and are auto-disabled after a period of repository inactivity (`VERIFY` on the day).
See Open Question 2.

### B4 · The date anchors are mutually contradictory · VERIFIED

Plan and spec are dated **2024-11-07** (filenames and `Status: approved`). The briefs were compiled
**2026-08-27**. The scope records hardcode `authorised_on: 2026-09-07`. Today is **2026-08-29**, and
weeks 1–12 of the programme have not started. Week 13 cannot begin on 2026-09-07.

For a project whose entire thesis is *"being checkable is the product"*, a public repo whose primary
evidence artifact carries a start date two years before the brief that commissioned it is the exact
own goal `00` B9 warns about. Not silently corrected here — document provenance is yours to set.

### B5 · Literal `USER` placeholders on the day-one path · VERIFIED

`copier copy gh:USER/backoffice-kit` (Task 1), `user_agent_contact: https://github.com/USER/selector-drift`
(Task 5). The second one is worse than a broken command: `00` §3.3 and brief §3.3 require the agent to
**identify itself with a working contact URL**. A 404 contact URL is a compliance claim that fails the
first time anyone checks it.

---

## CRITICAL — the product is not delivered, or a safety gate is defeated

### C1 · The pipeline is never assembled · VERIFIED

`selector_drift/orchestrate/flows.py` is created in Task 6, where `extraction_flow` deliberately
*"does nothing else — no cascade exists yet"*. It appears in no subsequent task's `Files:` list.
Tasks 13–33 build the cascade, the gate, extraction, the lake, quality and evidence; **nothing wires
them into the flow.** At Task 41 the DoD sweep asserts `rung_stats()["rung5"] == 0.0` and
`query("select count(*) from lake")` against a pipeline that has never run end to end.

Every downstream claim inherits this: the heal log has no heal events, the lake has no rows, the
rung distribution has no denominator. Revision R7 inserts **Task 26A** at the only correct point —
after the gate exists (Task 25) and before the suites measure it (Task 26).

### C2 · Cache write-back timing is undefined, and the wrong choice is silent and permanent · ASSESSED

`SelectorCache.put()` is described (Task 17) as *"the entire write-back. Self-healing is this line and
nothing else."* **The plan never says when it is called.** If it is called at heal time — the obvious
reading, and the one an implementer following Task 17 will write — then a heal that the gate
subsequently *quarantines* has already poisoned the cache. The next run gets a **rung-1 hit** on the
wrong selector; rung 1 records no heal; §5.7's strict path never engages; §5.2's anchor re-verification
passes because the anchor stored was the wrong one.

That is the adjacent-column failure, made permanent, through the one door the design worked hardest
to close. No test forbids it. Revision R11: `put()` is reachable only from the `ValidatedExtraction`
path, plus `test_a_quarantined_heal_does_not_update_the_cache`.

### C3 · The gate's acceptance path is untested, and a label rewording quarantines forever · ASSESSED

Trace the design's own tables for `LabelReword`, the single most common real UI change:

- Rung 2 (`axtree`, TEXT_SEMANTIC): accessible name no longer matches → `None`. Plan asserts exactly
  this (`test_axtree_fails_on_label_reword`).
- Rung 3 (`anchors`, TEXT_SEMANTIC): label text gone → `None`. Plan asserts this too.
- Rung 4 (`heuristics`, STRUCTURAL): heals via `data-testid`. Plan asserts this.
- `IndependentRungAgreement` (§5.6): healed by STRUCTURAL ⇒ confirmer must be TEXT_SEMANTIC ⇒ rungs 2
  and 3 ⇒ **both just failed** ⇒ no confirmer resolves ⇒ `INSUFFICIENT`.
- §5.7: `INSUFFICIENT` on a heal is treated as `FAIL` ⇒ **quarantine.**

So every label rewording quarantines. On the bench, mutations are re-seeded daily, so rewordings
recur with fresh strings — and `first_heal_policy: auto_promote` does **not** rescue it, because that
policy satisfies only `FirstHealConfirmation`; `IndependentRungAgreement` still fails. Design §14's
mitigation for "log stalls on quarantine backlog" therefore does not work.

The plan cannot see this because **it never tests that a benign heal is accepted.**
`tests/suite/test_mutation_heals.py` asserts resolution (`trace.winner in (2,3,4)`) and stops;
`test_adversarial_refused.py` asserts refusal. Nothing asserts acceptance. A resolver that quarantines
100% of legitimate heals passes every test in this plan and produces the headline
*"zero manual selector fixes"* only because a human resolved the queue by hand every night.

**Fix (revision R14), and it is already in the brief.** Brief §5 lists as a check: *"Cross-field
consistency — does the row still foot? **Do fields co-vary as they did historically?**"* The design
folded this into `CrossFieldAssertion`, and Task 21 implemented only the footing half
(`"net + tax == gross"`). The historical half is unimplemented and it is exactly the missing third
signal family:

> `Family.VALUE` — `RowKeyContinuity`: join this run's rows to the previous run's on the spec's
> natural key; for keys present in both, require the healed field's values to agree at or above a
> stated rate. Independent of both text and structure, deterministic, no model, no distribution.

Against `LabelReword` the values are unchanged → confirms → heal accepted. Against `DecoyColumn` the
"Prior Amount" values do **not** match last run's Amount for the same claim → refuses. It is a
*stronger* discriminator for the adjacent-column case than DOM agreement, because it looks at the
data rather than at the page. `IndependentRungAgreement` accepts a `VALUE`-family confirmer when no
DOM cross-family confirmer resolves; when neither resolves, `INSUFFICIENT` stands.

If you prefer not to change §5.6, the honest alternative is to accept it and say so: *"a label change
costs one human confirmation, once, and never again."* That is defensible and sellable — but then the
bench needs `RowKeyContinuity` anyway or the log stalls. Recommendation: take the fix.

### C4 · `rung_stats()` has no data source, and the schema is frozen on day one · VERIFIED

Task 6 promises `rung_stats() -> dict[str, float]`, *"proportion of resolutions won by each rung"*,
and Task 26 Step 6 requires its output (`rung1 82% · … · rung5 0.0%`) to be **a published claim, from
this command, not from an estimate** (B11). Task 41 asserts on it.

Task 3's schema records `heal_events` — i.e. **only the runs where a heal happened.** Rung-1 wins are
written nowhere. `rung1 82%` is not computable from any table in the plan.

This compounds with Task 3's own rule: *"none of them may add a column afterwards, because you cannot
compare day 1 to day 90 across a schema migration."* A gap here is not a week-19 fix; it is
**permanent**. Revision R5 adds a `resolutions` table on day one.

### C5 · "the same node" is undefined in the check the whole project rests on · ASSESSED

`IndependentRungAgreement` *"requires the two to point at the same node."* Both rungs return
`ResolvedElement(selector: str, anchor, rung_id)`. Comparing `selector` strings is wrong in both
directions: two different selectors routinely name the same node (`#claim` vs `td:nth-child(1)`), and
the same selector string can be produced against different snapshots. The sequencing note says
*"if only one thing in this plan is done carefully, it is cross-family confirmation"* — and its
equality test is unspecified.

Revision R10: `ResolvedElement` carries a `node_id: str`, minted by the page source as a stable
identity (live: an injected `data-sd-node` attribute read back after resolution; static: the element's
structural path in the parsed tree). Agreement compares `node_id`, never `selector`.

### C6 · Replay cannot work as specified — the import contract forbids it · ASSESSED

Three statements in the plan are jointly unsatisfiable:

1. `.importlinter` forbids `selector_drift.heallog` → `selector_drift.browser` (Task 1).
2. `heallog.replay` runs `resolve_cascade` against a stored DOM (Task 19).
3. `resolve`'s rungs take a `PageHandle`, which is defined in `browser/` (Task 4), and rung 2 needs an
   accessibility tree, which only a browser computes (Task 14).

import-linter's `forbidden` contract reports **indirect** imports by default, so
`heallog → resolve → browser` breaks contract 3 the moment Task 19 lands. And even suppressed, rung 2
cannot build an AX tree from a string without a browser.

This is not a lint problem, it is the missing abstraction that design §6's best idea depends on —
*"the mutation regression suite and the replay path are the same code."* Revision R9 introduces a
`PageSource` protocol in a neutral module with two implementations (live Playwright; static parsed
DOM with a computed accessible-name/role mapping). It fixes the contract, makes replay real, makes
mutation tests run without a browser, and — see P1 — is also the single largest performance win
available.

### C7 · The test bench's mutation engine cannot run in the test bench · VERIFIED

Tasks 8–11 define the mutation engine in **Python** (`testbench/mutations/*.py`,
`Mutation.apply(html: str) -> str`, *"implement the four transforms with `lxml`"*). Task 8 Step 3
serves them from **`testbench/portal/middleware.ts`**: *"The middleware calls
`mutations_for(today, 'testbench')` and pipes the rendered HTML through each mutation's `apply()`."*

A Next.js middleware is TypeScript on the Edge runtime. It cannot call Python. As written the bench
either serves unmutated pages or serves a **second, untested TypeScript implementation** of the
mutations — in which case the tested transforms and the served transforms are different code and the
answer key is not ground truth, which voids the treatment arm of the ninety-day log and the
open-source artifact's whole claim.

Three ways out; revision R6 takes the third. (a) Port mutations to TS — then the Python mutation tests
have nothing to test. (b) Serve the bench from Python — loses the realistic Next.js portal.
(c) **Keep one Python engine and put it in the request path** — a thin Python mutation service the
portal proxies its rendered HTML through, or a pre-render step that materialises the day's mutated
pages as static output. (c) preserves one implementation, keeps determinism from the seed, and keeps
`lxml`.

---

## HIGH — security

### H1 · The container hardening contradicts the baseline it cites · VERIFIED

Task 4 Step 4: `security_opt: [ "seccomp=unconfined" ]`. `00` §8 requires *"seccomp default"*, and
design §9.2 repeats it verbatim. `unconfined` **removes** the seccomp filter — the opposite of the
requirement — on the one container in the system that renders hostile HTML and holds session
credentials. It is there because Chromium's own sandbox needs privileges under the default profile;
the correct answers are a Chromium-specific seccomp profile, or `--no-sandbox` inside an already
locked-down container, chosen deliberately and written down.

Also missing from that service, all cheap: `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`,
`shm_size` (Chromium's default `/dev/shm` is 64 MB in Docker and is a well-known crash source — or
`--disable-dev-shm-usage`), and `PLAYWRIGHT_BROWSERS_PATH` — the image installs browsers as root while
the service runs as `10001:10001` with `read_only: true`.

### H2 · Nothing keeps the browser on the authorised target · ASSESSED

The scope gate's guarantee is *"there is no path to a page without a scope record."* True of the first
navigation. After that, a redirect, an SSO hop, a link click in a nav step, or an absolute URL in a
spec navigates anywhere, and the run is still inside an `AuthorisedTarget` context. The test meant to
prevent this (`test_no_public_function_accepts_a_bare_url`) only checks that no parameter is *named*
`url` and typed `str`; `href`, `path` or `target_url` pass it unchanged.

Revision R8: derive an origin allowlist from the `AuthorisedTarget`, enforce it with a Playwright
route handler that aborts any request to another origin, and test it with a page that redirects
off-origin. This is the enforcement the scope gate is *claimed* to have.

### H3 · The API has no authentication, and the audit trail is self-asserted · VERIFIED

Task 34 exposes `GET /heals/{id}` (redacted evidence, DOM snapshots), `GET /quality/{run_key}`
(extracted data) and `POST /quarantine/{run_key}/promote`. There is no auth of any kind anywhere in
the plan. Worse, `promote` takes **`actor` in the request body** — Task 25 tests
`row.actor == "me"` where `"me"` came from the caller.

DoD item 4 is *"quarantine with promote/discard and **full audit**"*, and the sales claim is that a
human confirmed a heal. An audit log whose `actor` is supplied by the party being audited is not an
audit log. Revision R16: session-derived actor, and the endpoint rejects an `actor` in the body.

### H4 · The rate limit is required in four places and enforced in none · VERIFIED

Brief §3.1 (scope record), brief §11 (*"rate limit enforced"* is a named test), design §4.3
(*"read from the `AuthorisedTarget`, never from config… not a tunable"*), design §12. In the plan it
appears once, as prose in Task 4 Step 3 (*"applies `target.rate_limit`"*), with no step and no test.

It also needs to be **persistent across runs**, not a per-run bucket: `1 req / 30 s` inside a run says
nothing about ten runs starting at once. Revision R8 adds the limiter, its persistence, and the test.

### H5 · `CrossFieldAssertion(expression: str)` is arbitrary code from a config file · ASSESSED

Task 21 evaluates `"net + tax == gross"` and does not say how. The default reading is `eval`. Today
the specs are ours; design §10 explicitly roadmaps screen 2 to *"an editor for non-engineers… the
trigger to build it"* — at which point a YAML file becomes remote code execution in the container
that holds client session cookies. Revision R13 pins a restricted evaluator (`ast`-walked whitelist or
`asteval`/`simpleeval`) and pins `yaml.safe_load` for every loader, neither of which is stated anywhere.

### H6 · Redaction protects PII and ignores credentials · ASSESSED

`bok_core.boundary.redact` detects *"emails, phones, tax ids, bank/routing numbers, addresses, person
names"* (`00` §4.2). Evidence is a **DOM snapshot and a screenshot of a logged-in portal**: it contains
session cookies if `storage_state` is captured alongside, `Authorization` headers if network state is
kept, CSRF tokens in hidden inputs, and the contents of `input[type=password]` when a login page is
snapshotted mid-flow. None of these look like PII and none are redacted. The highest-value secret in
the system is the one thing the redaction pass does not look for.

Revision R15 adds a credential-scrubbing pass ahead of PII redaction plus
`test_no_session_cookie_survives_into_stored_evidence`.

### H7 · `sensitivity` is a free-text field that gates publication · ASSESSED

`scope.yaml` carries `sensitivity: SYNTHETIC`. Task 33 stores `SYNTHETIC` evidence and asserts it is
*"publishable… with no redaction argument"* (design §8.2). Therefore **one wrong word in one YAML file
publishes a client's portal screenshots** — the precise outcome B4 exists to prevent, reachable by
typo, with no second control.

Revision R4: derive sensitivity from `basis` (`client-authorised` ⇒ `CLIENT_CONFIDENTIAL`, not
overridable), redact unconditionally regardless of label, and gate publication on an explicit
per-target allowlist rather than on the label.

### H8 · Four mismatches against the published substrate contract · VERIFIED

`INTERFACE-CONTRACT.md` is authoritative: *"If a symbol is not in this document, it does not exist,
and the plan task that needs it is blocked on an amendment here — not on a local workaround."*

| # | Contract says | Plan does |
|---|---|---|
| 1 | §1.5 `egress.guard_write(path, payload, plane)` is **"Required by `selector-drift`"** for the evidence write path, and *"consumers route their write paths through this rather than writing bespoke guards"* | Never mentions `guard_write` or `Plane`. Task 33 writes a bespoke guard. |
| 2 | §1.3 `redact(payload: Labelled[str]) -> RedactionResult`; §1.1 *"the label attaches where a file is opened"* | Task 33 has `store(evidence: Evidence, sensitivity: Sensitivity)` — the label is a sibling parameter, and `Evidence.dom/screenshot` are bare `str`/`bytes`. |
| 3 | §4 `Finding(id, severity, title, claim, taxonomy_refs, logical_location, partial_fingerprints, properties)`; `claim` is **required** | Task 29 asserts `f.rule == "row_count"` and `f.detail["threshold"]`. Neither `rule` nor `detail` exists. No `Claim` is constructed. |
| 4 | §2.2 `DomSource(snapshot_id, selector, rung)` exists specifically for this project | Task 27 asserts only `resolved.selector in str(claim.source)`. `snapshot_id` — the link from a claim back to the stored evidence, i.e. the provenance rail's whole point here — is never populated. |

Plus one omission: §5 `bok_core.testing.degraded.blackout()` and the row *"selector-drift · majority ·
mutation classes healed by rungs 1–4"* — see M3.

### H9 · No rung requires the element to be visible · ASSESSED

Rung 2 matches on accessible name. A page containing a hidden or off-screen element with
`aria-label="Claim Number"` — a stale template, a print-only block, a screen-reader helper, or a
hostile decoy — resolves ahead of the real one, and every value it yields may satisfy the format
assertion. The bench has exactly one adversarial mutation (decoy column) while the brief asks for
*"a set of adversarial mutations"*; this class is absent. Revision R12 adds a visibility precondition
to every rung and six further adversarial classes.

### H10 · The authorisation record is unsigned and its evidence unchecked · ASSESSED

`authorise()` refuses `CLIENT_AUTHORISED` when `evidence_uri is None`. It does not check that the URI
resolves, that the document is what it claims, or that the YAML has not been edited since. The scope
record is *"the document you would point at if anyone ever asked why you accessed a system"* (§4.3) —
so its integrity is the control, and right now the control is "trust the working tree".
Revision R3: record `evidence_sha256`, verify at authorise time, and write the scope record's own hash
into `audit_log` on every run.

---

## MEDIUM — completeness and verifiability

**M1 · "Zero manual selector fixes" is unfalsifiable.** The headline (design §8.3) is only a claim if
manual fixes are *recorded*. Nothing logs a human editing `specs/*.yaml` or a `selector_cache` row.
Revision R17 adds a manual-intervention ledger; without it the programme's flagship number has the
same epistemic status as the vendor numbers `00` was written to reject.

**M2 · No alerting exists.** Brief §4: *"Alert **before** bad data reaches the client's system."*
DoD 7: *"data-quality contracts **and alerting**"*. Task 29 produces `Finding` objects and nothing
sends them anywhere. A quarantined run at 04:12 notifies no one, so the ninety-day log stalls silently.

**M3 · No degraded-mode test.** `00` R3 names it (`test_degraded_mode_still_produces_findings`),
contract §5 sets this repo's floor (*"majority of mutation classes healed by rungs 1–4 under
`blackout()`"*), design §12 says it is *"kept as a regression guard"* — and the plan contains no such
test. `grep -ic degraded` = 0.

**M4 · `TaskSpec` has no expected schema.** Brief §4 defines a task spec as *"navigation steps,
semantic targets, **expected schema**, validation assertions, schedule, and scope reference."* Task 5's
`TaskSpec` is `version, scope_ref, navigate, targets, schedule`. Task 29 then tests `schema_drift`
against no baseline.

**M5 · There is no row model.** `read(element: ResolvedElement, spec: TaskSpec) -> CandidateExtraction`
takes one element and returns `rows: list[dict[str, Claim]]`. A results table is N rows × M fields and
the cascade resolves per field. How per-field results align into rows, and what the row's identity is,
is never stated — yet Task 28 upserts *"on the natural key"* and Task 21's `CrossFieldAssertion`
operates across a row. `natural_key` does not exist in any schema in the plan. This blocks C3's fix,
Task 28's idempotency, and Task 30's changeset diff.

**M6 · The idempotency scheme contradicts itself.** Design §9.1: *"a closed partition is never mutated
in place."* Task 28: `write(validated); write(validated)` must leave the row count unchanged, which
for *"one Parquet file per run"* means overwriting that file — mutating in place. Both cannot hold.
The standard resolution (append-only `{run_key}-{attempt}-{ulid}.parquet` plus a dedup view on the
natural key by latest `ingested_at`) satisfies both tests and the design; it needs to be written down.

**M7 · `Finding` field names are wrong** — see H8 row 3.

**M8 · `00`'s cross-cutting UI requirements are dropped without a scope call.** Verified absent:
the **provenance rail** (`00` §5.3 — *"the signature element, present in all four products, and the
reason a buyer remembers which vendor you were"*; §5.4 lists `Claim` and `ProvenanceRail` as v0.1
components; the contract's §7 row for this repo consumes them), the `cmdk` command palette (§5.5
*"full keyboard operation including"*), the print stylesheet (§5.5 *"for every report view"* — the heal
log is a report view and gets forwarded to auditors), 375px responsiveness, and the density modes.
Design §10 makes an explicit and well-argued scope call on screens 2 and 5; it makes none on these,
so they read as forgotten rather than cut. Task 27 makes every value a `Claim` and no screen renders
one.

**M9 · Nothing publishes the heal log.** DoD 6 is *"ninety-day heal log running since day one, with
**published** methodology"*; design §8.2 discusses publishability at length. No task produces the
public artifact. `00` B9 attaches obligations to publishing (methodology, stated sample, right of
reply, versioned results with a changelog) — only the methodology half is planned.

**M10 · The control arm's ground truth is not collected.** §8.1's claim is *"heals fired only on runs
following an actual deploy, **verified against deploy history**"*. No task ingests deploy history.
Without a stored deploy feed the control arm's entire claim is retrospective hand-checking.

**M11 · There is no remediation path for a real UI change.** After a permanent label rename, the
steady state is a nightly quarantine. Screen 2 is read-only in v1 by design, so the operator's only
route is editing YAML by hand — undocumented, and unlinked from the quarantine screen where the
operator actually is. This is the $1–5k/month maintenance retainer's daily work; it deserves a runbook
and a link.

**M12 · Load-bearing dependencies are never declared.** Task 1 Step 2's `uv add` line omits: `lxml`
(Task 9 Step 3 requires it by name), `cryptography` (Task 32's envelope encryption), `pytest-asyncio`
(Task 4 uses `@pytest.mark.asyncio`), `pytest-httpx` (Task 32 uses the `httpx_mock` fixture), `httpx`
(the broker makes HTTP calls). It *does* add `scipy` — ~100 MB of wheels for a statistic (PSI) that is
twenty lines of numpy, on a project whose stated constraint is `docker compose up` on a fresh machine.

**M13 · `runs.run_key` as PK destroys retry history and inflates the headline.** `run_key =
f"{target}:{spec_version}:{window}"` is deterministic by design (good, for idempotency), so a retry
after a failure overwrites the failed row. The published number is *"99.2% run success"*. Measuring
success on a table that silently discards failures is the failure mode this programme exists to
criticise. Needs a `run_attempts` child table — and the schema is frozen on day one.

**M14 · `selector_cache` PK omits `spec_version`.** `(target, field)` only. Change a `FieldTarget`'s
`description` — i.e. change what the field *means* — and the next run gets a rung-1 cache hit on a
selector resolved for the old meaning, with the old anchor verifying happily. Frozen on day one.

**M15 · No indexes are specified.** `runs.started_at` (the timeline), `heal_events.run_key`,
`heal_events.target`, `resolutions.run_key`. Cheap now, a migration later, and Task 3's own rule bans
migrations.

**M16 · The PSI specification is not reproducible.** `DistributionalAssertion(threshold=0.20)` does not
state the binning strategy (quantile? fixed? how many?), the zero-bin epsilon (PSI is undefined at
`log(0)`), the reference window (all history? trailing N? same weekday?), or **which fields get a
distributional check at all** — PSI over a high-cardinality identifier column is noise. The heal-log
line the design is proud of, *"distribution unchanged (PSI 0.03, threshold 0.20, n=847)"*, is not
reproducible by a reader, which is the one thing this programme promises.

**M17 · `persistence=2` disables the check on the only run that matters.** Persistence-across-runs is
the right cure for alert fatigue (B7) on the *alerting* path. On the *blocking* path it means a heal
whose distribution shifted immediately gets `PASS` on run one — and
`test_seasonality_does_not_alert_before_persistence_is_met` asserts exactly that. Design §5.7's
argument (*"a heal into a shifted distribution blocks"*) is contradicted by the plan's own test. Split
the semantics: persistence for alerting, single-run breach for blocking on a heal.

**M18 · `warmup_runs=30` on a daily cron is 30 days of quarantining every heal.** Combined with §5.7
(*"any heal during warm-up quarantines — correct, because there is no basis to trust it yet"*, which is
right in principle) it means the treatment arm quarantines every heal for the first month of the
ninety-day log, and `auto_promote` does not help — same mechanism as C3.

**M19 · Five tests pass without testing anything.**
`test_broker_fetches_a_code_and_stores_nothing` asserts `not list(tmp_path.rglob("*"))` on a `tmp_path`
the broker was never told about · `test_redaction_failure_writes_nothing` does the same to `store()` ·
`test_changeset_opens_no_network_connection` greps source text for three substrings and passes if you
import `socket` · `test_no_public_function_accepts_a_bare_url` (H2) ·
`test_dod_06_log_running_since_day_one` asserts `>= 1` day.

**M20 · Two time bombs.** `test_all_three_day_one_targets_authorise` calls
`authorise(..., today=dt.date.today())` against records with `expires_on: 2027-09-07`: green CI until
that date, red after, for no reason anyone will remember. `dt.date.today()` is also local-time — the
run window, the mutation seed and the expiry check must all agree on UTC or the bench's answer key
drifts by a day at the boundary.

**M21 · `.importlinter` needs `include_external_packages = True`.** The `no-model-calls` contract names
`bok_core.gateway`, which is outside `root_packages`. Without that option the contract does not match
the module and reports "kept" for the wrong reason — a green tick with no test behind it, which
`00` §7 names explicitly as *"a lie with a green tick"*.

**M22 · A claimed contract is not implemented.** Design §3.4 lists *"`validate/` may reach rungs only
via the public `Rung` protocol — §5.6's independence must be structural."* `.importlinter` has five
contracts and none of them express it.

**M23 · Rung 1 must run format assertions, and is forbidden from importing them.** Task 17: the cache
hit requires *"the element satisfies the target's format assertions"*. `FormatAssertion` lives in
`validate/assertions/format.py` (Task 21), and contract 2 forbids `resolve` → `validate`. The matcher
must live in `spec/` and be used by both.

**M24 · `db/` is missing from the scaffold.** Task 1 Step 6's `mkdir -p` list omits it; Task 3 creates
`selector_drift/db/models.py` into a directory with no `__init__.py`.

**M25 · One adversarial mutation, where the brief asks for a set** and the plan's own sequencing note
says this is the set that matters. Proposed additions in R12: hidden decoy with a matching accessible
name (H9) · duplicate label (two "Claim Number" headers, so "exactly one" fails correctly) · value
swap between two same-format columns (no structural signal at all — only `RowKeyContinuity` catches it)
· locale flip (`1,234.56` → `1.234,56`: passes format, breaks domain) · virtualised table rendering 10
of 100 rows (row-count contract) · header supplied as an image with `alt` text.

**M26 · No test tiering.** *The full check* runs `pytest -q --cov` over a suite that launches Chromium,
hits Postgres, hits a deployed bench and shells out to `lint-imports` three times, and it is the gate
after most tasks. On a 300-hour solo budget the suite becomes the bottleneck and then gets skipped.
Markers (`unit`/`integration`/`e2e`), `pytest-xdist`, a session-scoped browser, and coverage measured
on the fast lanes fix it for an hour's work.

**M27 · A verification step that cannot verify.** Task 12 Step 2:
`TESTBENCH_DATE_OVERRIDE=2026-09-10 curl -s "$BENCH_URL/results"` sets an environment variable for
`curl`, which does not forward it to a remote server. Two identical hashes will be read as "mutation
works" or worked around. Needs a header or query parameter the deployed app reads.

**M28 · Vercel Hobby is non-commercial** (`VERIFY` on the day). The bench is a sales asset for
$15–60k engagements and design §7 puts it on Vercel's free tier. This is `00` B3's own trap —
*"a licence breach that a sophisticated buyer's counsel could find"* — in the artifact you hand that
buyer. Cloudflare Pages/Workers free tier does not carry the restriction.

**M29 · The mint control is a naming convention.** `_ScopeToken` is module-private only by convention;
`from selector_drift.scope.record import _ScopeToken` works. The test only covers the *missing
argument* case, which is a typo guard, not the guarantee §3.2 claims. Real enforcement is cheap: put
the token in its own module and add an import-linter contract, plus assert token *identity* against a
singleton minted in `gate.py`.

**M30 · No retention or deletion policy.** `00` B7 requires *"a documented retention and deletion
policy"*. Ninety days of DOM snapshots and screenshots per heal, plus the lake, on an unspecified host
(B3), with no expiry.

**M31 · `test_promotion_re_runs_the_gate` patches the wrong name.** It monkeypatches
`selector_drift.validate.gate.gate`; `quarantine.py` will have bound the function at import
(`from .gate import gate`), so the patch is invisible and the test passes whether or not promotion
re-runs the gate — on the check that stops a human from waving data through.

---

## LOW

**L1** Filenames and `Status: approved 2024-11-07` vs a 2026 programme (see B4).
**L2** `test_diff_is_word_level_not_line_level` expects `remove == ["class=a"]` from
`<td class=a>X</td>`, which presumes an HTML-aware tokenizer the plan never specifies; whitespace
splitting yields `class=a>X</td>`.
**L3** `keyboard.spec.ts` loops `while (!focused) Tab` with no bound — a missing button hangs to the
suite timeout instead of failing.
**L4** Three separate tests shell out to `uv run lint-imports` (Tasks 1, 18, 19), each re-resolving
the environment, and `uv run` nested inside `uv run` is asking for a lock contention stall. One test,
or the `importlinter` API.
**L5** `uv run gitleaks detect --no-git` depends on a Go binary that is never declared or installed.
**L6** Design §5.1's table says rungs 2 and 3 are *"fooled by"* label rewording; the plan's tests
assert they **fail** on it. The distinction matters, because §5.6's justification is built on the
"fooled identically" reading.
**L7** `compact()` rewrites Parquet files under a live DuckDB glob with no atomic swap and no stated
trigger threshold — a reader can hit a file that just vanished.
**L8** Task 1 Step 6 touches `__init__.py` into `testbench/portal/`, making a Next.js app with
`node_modules` a Python package that grimp will walk on every `lint-imports`.

---

## Performance and efficiency

**P1 · Snapshot once, resolve many — the one change that matters.** As designed, each field runs each
rung against a live `PageHandle`, and every rung call is one or more CDP round-trips. Five fields ×
four rungs × N rows is hundreds of round-trips per run, and `IndependentRungAgreement` doubles it on
every heal. Capture **one** DOM + accessibility snapshot per page, then run every rung and every field
against that in-process. This is the same `PageSource` abstraction C6 needs for replay: one change
buys correct replay, browser-free mutation tests (seconds instead of minutes), and an order-of-
magnitude cut in run time. Take it before Task 13, not after.

**P2 · `scipy` for one statistic.** See M12. PSI is `sum((a - e) * log(a / e))` over binned
proportions.

**P3 · Missing indexes on a frozen schema.** See M15.

**P4 · Small-file growth is unbounded in practice.** One Parquet file per run × 3 targets × 90 days
plus retries, with compaction present but untriggered and unthresholded (L7). The design names the
small-file problem and then leaves the compaction policy blank.

**P5 · Test suite runtime.** See M26.

---

## What is right, and should not be touched in revision

Recorded because a revision pass that only lists problems invites over-correction.

- **Dropping `public-permissive`** (design §4.2). The single best decision in the document. It removes
  the entire contested-law surface for the price of a feature nobody asked for.
- **Rung 1 verifying anchor provenance, not the selector** (§5.2). This is the non-obvious insight the
  product turns on, and the plan's
  `test_column_reorder_fails_rung_one_even_though_the_selector_still_matches` is the right test with
  the right comment.
- **Refusing a confidence score** (§5.4). Correct, brave, and better collateral than `0.94`.
- **The `INSUFFICIENT`-on-heal asymmetry** (§5.7). This is what makes B7 tractable, and nothing in this
  audit changes it.
- **`Verdict` instead of a boolean**, carrying its own threshold. It makes "no bare red badges"
  structural rather than a UI review comment.
- **`test_decoy_column_is_refused_by_the_gate` asserts the *specific* verdict**, not merely that the
  run quarantined. That is the difference between a test and a coincidence, and most plans get it
  wrong.
- **`first_heal_policy` living on the scope record** rather than in config (§5.8).
- **Freezing the schema on day one.** Right for the reason given — which is precisely why C4, M13,
  M14 and M15 have to be fixed *before* day one rather than after.

---

## Revisions applied to the plan and design

| # | Fixes | Where |
|---|---|---|
| R1 | B1 — day-one blocking surface narrowed to `Sensitivity` + `Claim`/`DomSource`; the rest of `bok-core` deferred to weeks 17–18 with a named drop-dead date | Plan Global Constraints; Task 1; Task 2 |
| R2 | M12, M21, M22, M24, L8, P2 — dependency list, `.importlinter` correctness, scaffold | Task 1 |
| R3 | H10, M20, M29 — scope-record integrity, UTC, real mint enforcement | Task 2 |
| R4 | H7 — sensitivity derived from basis, never author-chosen | Task 2, Task 33 |
| R5 | C4, M13, M14, M15 — `resolutions`, `run_attempts`, `deploys` tables, cache PK, indexes. **Day-one, schema-frozen** | Task 3 |
| R6 | C7 — one mutation engine in the request path | Task 8, Task 12 |
| R7 | C1 — Task 26A assembles the pipeline | new Task 26A |
| R8 | H2, H4 — origin allowlist and persistent rate limiter, both tested | Task 4 |
| R9 | C6, P1 — `PageSource` protocol, live + static, snapshot-once | Task 13, Task 19 |
| R10 | C5 — `node_id` identity | Task 13, Task 22 |
| R11 | C2 — cache write-back only from the validated path | Task 17, Task 26A |
| R12 | H9, M25 — visibility precondition; six further adversarial classes | new Task 11A |
| R13 | H5 — restricted expression evaluator, `yaml.safe_load` | Task 21, Task 5 |
| R14 | C3, M5 — `natural_key`, `Family.VALUE`, `RowKeyContinuity`, and the acceptance suite | Task 5, Task 22, new Task 26B |
| R15 | H6, H8 — credential scrubbing, `Labelled`, `egress.guard_write` | Task 33 |
| R16 | H3 — API authentication and session-derived actor | Task 34 |
| R17 | M1, M2, M9, M10, M11 — manual-intervention ledger, alerting, published log, deploy history, runbook | new Task 29A, new Task 40A |

---

## Open questions — answered 2026-08-29

All four were put to the author and answered the same day. The answers are recorded here beside the
question they close, and applied in the plan and design; the plan's Revision log carries the
cross-reference table.

| OQ | Answer | Consequence |
|---|---|---|
| 1 | **Out of programme order — rebase the dates.** Selector Drift runs first | Every week number is project-relative; no calendar date is assumed anywhere. B4 is closed. **B2 is not** — the control arm still needs two self-owned targets named, now recorded as precondition P-3 |
| 2 | **A free always-on VM** — Oracle Cloud Always Free or Fly.io | B3 closed. Adds three container constraints the plan did not have: `arm64` image, UTC host clock, and the idle-reclaim policy as a documented gap |
| 3 | **Take the `Family.VALUE` amendment** to design §5.6 | C3 closed. The conservative alternative is rejected on the ground that the bench re-seeds daily, so "one confirmation per label change" is in fact one per night |
| 4 | **Move the bench off Vercel** onto the same VM | M28 closed, and `00` B6 improves — one free-tier quota on the nightly path instead of two |

**One question stays open, and it is the one that blocks day one: P-3.** OQ1's answer means
`agent-perimeter` and `ledger-sense` are roughly twelve weeks away, so the ninety-day log's control arm
has no targets. Two self-owned, already-deployed applications whose deploy history is readable must be
named before Task 5.

### The questions as originally put

**1 · Is Selector Drift starting now, out of programme order?** Nothing is built in any of the five
repos (VERIFIED). The design's answer to brief Q1 makes `agent-perimeter` and `ledger-sense` demo
deployments the ninety-day log's control arm, and neither exists. Either (a) weeks 1–12 run first and
week 13 is ~12 weeks out, so every hardcoded date in the plan is wrong, or (b) this project runs
first, in which case the control arm needs two different self-owned targets named now, before day one.
Both are legitimate. The plan currently assumes (a) while its dates assume (b).

**2 · What machine runs the cron for ninety days?** Not stated anywhere. Laptop, free always-on VM,
or GitHub Actions — each changes Task 6, the container hardening, the retention policy, and the
honesty of the log's continuity claim.

**3 · Take the `Family.VALUE` / `RowKeyContinuity` fix, or accept "one human confirmation per label
change"?** C3 makes the current design quarantine every label rewording forever. The fix is small,
deterministic, already implied by brief §5, and strengthens the adjacent-column defence — but it
amends design §5.6, which is the document's most carefully argued section, so it is yours to sign off.
The revisions implement the fix and mark it; reverting it is a one-line policy change if you disagree.

**4 · Does the bench move off Vercel?** M28. If Hobby's non-commercial terms apply to a sales asset
(VERIFY), Cloudflare Pages is the same money and no argument.
