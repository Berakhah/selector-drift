# 03 — Selector Drift

**Self-healing extraction for business systems with no API.**

Weeks 13–20 · Repo: `selector-drift` · Accent: drift indigo
Depends on: `00-SHARED-FOUNDATION.md`

---

## HOW TO USE THIS DOCUMENT

```
Read 00-SHARED-FOUNDATION.md and then 03-SELECTOR-DRIFT.md, both in full.

Use the superpowers skills in order: brainstorming → using-git-worktrees →
writing-plans → subagent-driven-development → test-driven-development →
requesting-code-review → verification-before-completion.

This is the highest-ticket project and the one with the most ways to cause real
harm. Two things dominate the design and must be settled before any plan task:

  1. Self-healing's failure mode is worse than breaking. A broken selector throws.
     A wrongly-healed selector silently returns plausible, incorrect data into a
     client's system. Section 5 is the answer and it is not optional.

  2. Legal scope. Section 3 and Section 10, B1. The tool refuses to run against a
     target with no authorisation record. Build the refusal before the crawler.

Section 13 lists what I have deliberately left open. Ask, do not assume.
```

---

## 1. MISSION

A pipeline that describes what it wants in words instead of CSS paths, re-resolves the page when the UI changes, and produces a log proving it healed itself.

One sentence for a buyer: *"Your integration breaks every time that portal ships a UI change. Mine re-resolves the element, proves in a log that it healed correctly, and alerts you before bad data reaches your system."*

---

## 2. THE EVIDENCE — JOB MARKET, NOT STATISTICS

Two claims from the source report's first edition were removed and must not be reintroduced:

- **"Hundreds of thousands of business-critical systems with no API."** No source, and none could exist — there is no census of internal enterprise systems, and both "business-critical" and "no API" are undefined.
- **"15–20 minutes per claim down to under 90 seconds."** A vendor self-report that could not be located.

What replaces them is stronger because it is first-hand observation of live listings, sampled 22–27 August 2026:

- *"Senior Web Scraping Engineer — Python, Browser Automation, Production Data Pipelines"* — $50–82/hr, 50+ proposals.
- *"Python Data Engineer — Resilient API & Web-Scraping Pipelines into a Parquet/DuckDB Lake (Prefect)"* — **$70,000 fixed, only 20–50 proposals.**
- *"Python Lead Gen Scraper — Serper + OpenAI Validation (n8n Migration)"* — hourly, expert.
- *"Create robust web scrapers with automatic updates."*

You do not need a market-size statistic when you can read the demand directly off the board. Note the second listing especially: high budget, well-specified, low proposal count. The source report's own positioning rule — *competition tracks vagueness and low stated budgets, not opportunity size* — is visible in that one line.

**The failure modes clients describe are consistent and specific,** and they are the wedge:
- Direct database access violates vendor licence agreements.
- File and EDI transfer is hours or days stale.
- RPA breaks on any UI update and needs dedicated machines.
- Hand-written browser automation means someone maintains selectors, and every UI change becomes a backlog ticket.

That last one is what you sell against.

---

## 3. HARD CONSTRAINTS SPECIFIC TO THIS PROJECT

1. **No target runs without a scope record.** `scope.yaml` per target, containing: target identity, the authorisation basis (client-owned portal with client credentials / public data with permissive terms / explicit written permission), the authorising party, a date, and a rate limit. The runner refuses unknown targets. Fails closed, tested.
2. **Default posture is the client's own authenticated access to a portal they already pay for.** That is where the $15–60k engagements live, it is legally clean, and it is the higher-value work anyway. Write the distinction into every proposal — it signals seniority.
3. **For third-party public sites:** public data only, respect terms and `robots.txt`, no circumventing authentication, no defeating anti-bot measures, identify the agent with a contact URL, rate-limit conservatively.
4. **No credential is ever stored in plaintext.** Envelope encryption, per-client keys, encrypted at rest, never logged, never in a screenshot. See B2 for the TOTP question, which is thornier than it looks.
5. **Healed selectors do not write data until validated.** §5.
6. **Every screenshot and DOM snapshot is redacted before storage** and never leaves the client's boundary. A portal screenshot is `CLIENT_CONFIDENTIAL` (`00`, R4) and cannot reach a free-tier vision model.

---

## 4. WHAT YOU BUILD

**Semantic task spec.** Targets described in language — *"the claim number cell in the results table"* — not by DOM path. A task spec is a versioned YAML artifact: navigation steps, semantic targets, expected schema, validation assertions, schedule, and scope reference.

**The resolver — the whole product.** Cache a concrete selector for speed. On failure, fall back to a re-resolution cascade (§5). Write the new selector back to cache. **Self-healing is that write-back and nothing else** — everything valuable is in how you decide the new selector is correct.

**Heal log.** *"Selector X broke at 04:12, healed to Y, confidence 0.94, validated by assertions A/B/C"* with before-and-after screenshots (redacted) and DOM snapshots. This artifact is what you show clients. It is the difference between a scraper and a system.

**Orchestration.** Retries with backoff, rate limiting, session and MFA handling, scheduled runs, per-run resource accounting. On free tiers, "cost" is quota — track and display it as such.

**Typed landing zone.** Parquet + DuckDB with schema-drift detection and a data-quality contract: row counts, null rates, distribution shift, referential checks. Alert **before** bad data reaches the client's system. Silent bad data is the failure clients fear most and vendors never address.

---

## 5. THE RESOLUTION CASCADE — AND WHY IT IS SAFETY-CRITICAL

The headline feature is also the worst failure mode. A broken selector raises an error and someone fixes it. A selector healed to the *wrong* element returns a plausible number into a client's accounting system, silently, for weeks.

**Cascade, cheapest and most reliable first:**

1. **Cached selector.** Verify it still matches exactly one element and that the element satisfies the target's assertions.
2. **Accessibility tree.** Role + accessible name + relationship. The most semantically stable layer of a page and the one most automation ignores.
3. **Text anchors and structural relations.** "Value in the cell to the right of the label 'Claim Number'." Survives most restyling.
4. **Attribute and structural heuristics.** `data-testid`, stable id patterns, table header position, column index with header verification.
5. **Visual grounding via a model.** Last resort. **Only ever on a redacted screenshot, and only where the client has opted in** — otherwise this rung is disabled and the task escalates to human review instead.

**Post-heal validation gate — mandatory, before any write:**

- **Type and format assertions** declared in the task spec. A claim number that matches `^[A-Z]{2}\d{8}$` is a much stronger signal than any model confidence.
- **Domain assertions.** Range checks, enum membership, date plausibility, currency sanity.
- **Cross-field consistency.** Does the row still foot? Do the fields co-vary as they did historically?
- **Distributional check against history.** A field whose values suddenly shift distribution is a heal that went wrong, even if every individual value looks valid. This catches the case where the resolver locked onto the adjacent column — the most common and most dangerous silent failure.
- **First-heal human confirmation.** The first time a target heals to a new selector, the run completes into quarantine and a human confirms before data is promoted. Subsequent heals to the same selector run automatically. This is a small amount of friction that converts the product's scariest property into its most trusted one.
- **Heal confidence gates writes.** Below threshold: quarantine, alert, do not write. Never guess into a client's system.

**Quarantine, not failure.** A quarantined run keeps its data, its evidence and its diff, and offers one-click promote or discard. A client who sees the system refuse to write questionable data trusts it more than one that never refuses.

---

## 6. THE TEST BENCH — AN OPEN-SOURCE ARTIFACT AND A SALES ASSET

You cannot demonstrate self-healing without pages that change, and you cannot ethically hammer third-party sites for ninety days to get them.

**Build a deliberately mutating web application** and ship it as part of the repo: a realistic portal (login, search, results table, detail page, export) with a mutation engine that applies parameterised UI changes on a schedule — class renames, DOM restructuring, label rewording, column reordering, framework-level markup changes, id churn, table-to-div conversion, pagination changes, and a set of adversarial mutations designed to induce *wrong* heals (a decoy adjacent column with plausible values).

This gives you:
- A reproducible heal-log with known ground truth, which is far stronger evidence than a log against sites nobody can inspect.
- A regression suite for the resolver.
- A published open-source artifact that makes your claims checkable — the thesis of the whole programme.
- Something no competitor has, because building it is unglamorous.

**The ninety-day live log** then runs against a small number of ethically clean targets: your own deployed applications, the test bench, and any consenting client portal. Start it on day one of this phase — it takes calendar time you cannot compress.

**The pitch, once you have it:** *"Ran 90 days across 3 portals, 41 UI changes, zero manual selector fixes, 99.2% run success, 2 quarantined runs correctly refused."* Your own measured data, not someone's marketing, and instantly legible to anyone who has been burned by RPA. Note that the quarantine count belongs in the headline — it is proof the safety mechanism works, not an admission.

---

## 7. ARCHITECTURE

```
selector_drift/
  spec/          task spec schema, versioning, validation
  scope/          scope.yaml schema, authorisation gate  ← build first
  browser/        Playwright driver, containerised, session + MFA handling
  resolve/
    cascade.py    the five rungs
    cache.py      selector cache with provenance
    validate.py   post-heal validation gate
    quarantine.py
  extract/        typed extraction into Pydantic schemas
  quality/        schema drift, null rates, row counts, distribution shift
  land/           Parquet writer, DuckDB catalog, idempotent upsert
  orchestrate/    Prefect 3 flows, scheduling, retries, quota accounting
  heallog/        heal events, evidence storage, redaction, diffing
  api/            FastAPI
testbench/        the mutating portal + mutation engine  ← shipped artifact
web/              Next.js 15 + bok-ui
```

**Storage note that will bite you:** DuckDB does not support concurrent writers across processes. Do not architect around a shared DuckDB file written by parallel flows. Write per-run Parquet, union at read time, and use DuckDB as the query layer over a partitioned lake — with compaction to avoid the small-file problem. Postgres holds run history, heal events and the selector cache.

---

## 8. UI/UX SPECIFICATION

Light-first editorial instrument. Indigo used for the heal state only.

**Screens**

1. **Targets.** Each target with its scope record surfaced prominently — authorisation basis, authorising party, date, rate limit. A target without a scope record renders as locked with the reason stated. The compliance posture is visible, not buried; it is a selling point.
2. **Task spec editor.** Semantic targets in plain language with a live preview against a captured page, assertions declared alongside each field, schema shown as it will land. Monaco for the YAML with schema validation, plus a form view for non-engineers.
3. **Run timeline.** Runs as a horizontal timeline: green completed, indigo healed, amber quarantined, red failed. Click into any run for its full record.
4. **Heal log — the signature screen.** A heal event rendered as a two-column before/after: screenshot left and right with the resolved element outlined, a word-level DOM diff beneath, the cascade rung that succeeded, every validation assertion with its result, and the promote/discard decision with who made it and when. The one orchestrated motion in the product: on open, the outline draws itself around the element over 220ms, first on the "before" then on the "after," so the eye tracks the move. Disabled under `prefers-reduced-motion`.
5. **Data quality.** Contract results per run: row counts against expectation, null rates, distribution shift with the actual distributions plotted, schema diff. Every alert states its threshold and why that threshold — no bare red badges.
6. **Quarantine.** Held runs with their data previewable, the reason stated, and promote/discard. Empty state reads *"Nothing quarantined"* with the count of runs that passed validation, not a celebration.

---

## 9. COMMERCIALS

| Offer | Price |
|---|---|
| Per integration | $15–60k |
| Maintenance | $1–5k / mo |

**Why that clears:** the alternative is one to three full-time data-entry staff.

**Post it as:** *"Production data pipeline with self-healing extraction and data-quality contracts."* Not "scraping." The framing selects the buyer, and the source report's own data shows the same technical work priced an order of magnitude apart depending on the frame.

---

## 10. BLINDSPOT REGISTER

**B1 — Legal scope is genuinely complicated and varies by jurisdiction and by target.** Terms of service, computer-misuse statutes, database rights, the EU's text-and-data-mining opt-out regime, and data-protection law if personal data is involved — all can apply, and US case law on public-data scraping is narrower than the internet's summary of it. Consequences: default to client-owned authenticated access; require a scope record; never circumvent authentication or anti-bot controls; and get your standard terms reviewed before the first paid engagement. Do not let a client tell you verbally that it is fine — get the authorisation in the scope record, in writing.

**B2 — You will be asked to store client portal credentials, and possibly TOTP seeds.** Storing a TOTP seed defeats the second factor and may breach the client's own security policy or the portal vendor's terms — even when the client asks you to. Preference order: (1) client-supplied session/cookie injection at run time; (2) a credential broker the client controls; (3) delegated access if the portal supports it; (4) stored credentials with envelope encryption and per-client keys, as a last resort with written acknowledgement. Raise this in the first call. Handling it well is a seniority signal; handling it badly is a breach.

**B3 — Silent wrong heals (see §5).** The defining risk. If you build nothing else from this brief, build the validation gate.

**B4 — Screenshots of client portals are client data.** They contain names, amounts, claim numbers. They cannot go to a free-tier vision model, they must be redacted before storage, and they must not appear in a public heal-log demo. Your published heal log runs against the test bench and your own applications for exactly this reason.

**B5 — Non-determinism makes the heal log hard to trust, including by you.** Store enough to replay: the DOM snapshot, the redacted screenshot, the cascade decisions, the model call ids and response fingerprints. A heal you cannot reproduce is an anecdote.

**B6 — Detection and blocking.** Managed bot-defence services will notice a browser automation running on a schedule. On client-owned portals this is usually solvable by talking to the vendor or the client's IT. On third-party sites, escalating your evasion is exactly the wrong direction — it converts a technical question into a legal one. If a target requires evasion to access, it is out of scope. Say that in the proposal.

**B7 — Distribution-shift alerting will cry wolf.** Seasonality, month-end, holidays and genuine business change all shift distributions. Use a proper statistic (PSI or a KS test) with a documented threshold and a warm-up period, require persistence across runs before alerting, and let the client mark an alert as expected so it learns. An alerting system a client mutes is worse than none.

**B8 — Playwright in containers is fiddly.** Browser dependencies, ARM vs x86 image differences, memory ceilings under parallel contexts, zombie processes. Pin the browser version, run one context per run, set hard memory limits, and reap aggressively. Also budget for the fact that a scheduled runner must survive host restarts — Prefect's own persistence, or a supervised process, not a `while true` loop.

**B9 — Idempotency and late data in the lake.** Re-running a window must not duplicate rows. Use a deterministic run key plus an upsert on a natural key, and handle records that appear later than expected without corrupting a closed partition. Test by deliberately replaying a run.

**B10 — The ninety-day log takes ninety days.** It is the primary sales asset and it cannot be compressed. Start it on day one of week 13, before the product is finished, running whatever exists. A partial log with an honest start date beats no log.

**B11 — Free-tier quota during a live run.** The visual-grounding rung is a model call. A scheduled 04:12 run that hits a daily cap will fail in a way that looks like a product defect. Cascade rungs 1–4 must carry the overwhelming majority of resolutions; instrument what proportion actually reach rung 5 and publish it.

**B12 — Scope creep into RPA.** Clients will ask you to *write* into the portal, not just read. That is a different risk class entirely — a wrong write into a client's system of record is unrecoverable in a way a wrong read is not. Decide your position now and price it separately if you take it at all.

---

## 11. TEST STRATEGY

- **Mutation suite.** Every mutation class in the test bench has a test asserting the cascade heals correctly, and every adversarial mutation has a test asserting the validation gate *refuses* the plausible-but-wrong heal. The second set matters more than the first.
- **Scope gate tests.** Unknown target refused. Expired authorisation refused. Rate limit enforced.
- **Credential tests.** No plaintext at rest, nothing in logs, redaction verified on screenshots and DOM snapshots.
- **Quarantine tests.** Below-threshold heals never write. Promotion path works and is audited.
- **Idempotency tests.** Replay a run, assert no duplicates.
- **Data-quality tests.** Synthetic distribution shift detected; synthetic seasonality not falsely alerted after warm-up.
- **Degraded mode.** With all model providers disabled, rungs 1–4 still heal the majority of mutation classes.
- **E2E:** Playwright driving the UI (a browser automation tool tested with browser automation — keep the fixtures separate).
- **Accessibility:** axe, zero serious/critical.

---

## 12. DEFINITION OF DONE — WEEK 20

1. Scope gate refuses unauthorised targets, proven by test.
2. Resolution cascade implemented, all five rungs, with rung-level instrumentation.
3. Post-heal validation gate implemented; adversarial mutations are refused, proven by test.
4. Quarantine with promote/discard and full audit.
5. Test bench shipped, open source, with its mutation engine documented.
6. Ninety-day heal log running since day one of the phase, with published methodology.
7. Typed landing zone with data-quality contracts and alerting, idempotent under replay.
8. Credentials handled per B2 with the chosen posture documented in `docs/security.md`.
9. Heal-log screen renders before/after with element outlines, DOM diff and assertion results.
10. `docs/methodology.md` states what the heal-log numbers mean: what counts as a UI change, what counts as a successful heal, what the sample is.

---

## 13. OPEN DECISIONS — ASK ME THESE

1. **Which three targets does the ninety-day log run against?** They must be ethically and legally clean, and I need them named before week 13 day one.
2. **What is my credential posture (B2)** as a default offer? Session injection is safest and hardest to sell; stored credentials are easiest to sell and worst to hold.
3. **Do I take write-back work (B12) at all,** and if so at what price and with what liability terms?
4. **Prefect 3 or Temporal?** Prefect is lighter and matches what the live job listings actually ask for; Temporal is more durable. The listings arguably decide this.
5. **Is the test bench a public repo or bundled in this one?** Public is a better artifact and a possible standalone reputation piece; bundled is less to maintain.
6. **How much of the visual-grounding rung do I build now** given that free-tier vision is the most fragile lane I have and cannot see client data anyway? An argument exists for shipping rungs 1–4 only in v1 and being honest that rung 5 is opt-in.
7. **Do I need my terms reviewed by a lawyer before the first engagement?** My instinct is yes for this project specifically, more than for the other three.
