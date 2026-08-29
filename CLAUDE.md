# Selector Drift

Self-healing extraction for business systems with no API.
Weeks 13–20 · Accent: drift indigo `oklch(0.55 0.14 268)` · Licence: Apache-2.0 (pending `00` §12 Q6)

**Pitch:** *"Your integration breaks every time that portal ships a UI change. Mine re-resolves the element, proves in a log that it healed correctly, and alerts you before bad data reaches your system."*

## Source of truth

- `../Starting_Documents/00-SHARED-FOUNDATION.md` — the substrate. Read in full before anything.
- `../Starting_Documents/03-SELECTOR-DRIFT.md` — this project's brief. Read in full.

The briefs win over anything summarised here. This file exists so a fresh session cannot violate a hard rule by accident.

## Workflow

superpowers, in order: `brainstorming` → `using-git-worktrees` → `writing-plans` → `subagent-driven-development` → `test-driven-development` → `requesting-code-review` → `verification-before-completion`. TDD is RED-GREEN-REFACTOR, no exceptions.

## The two facts that dominate the design

1. **Self-healing's failure mode is worse than breaking.** A broken selector throws and someone fixes it. A wrongly-healed selector silently returns plausible, incorrect data into a client's system for weeks. The validation gate is the product; the write-back to cache is trivial.
2. **Legal scope is genuinely complicated.** Build the refusal before the crawler.

## Never (each has a test proving it)

1. **No target runs without a scope record.** `scope.yaml` per target: identity, authorisation basis (client-owned portal with client credentials / public data with permissive terms / explicit written permission), authorising party, date, rate limit. Unknown target → refuse. Expired authorisation → refuse. Fails closed. **Build `scope/` first.**
2. **Never circumvent authentication or defeat anti-bot measures.** A target that requires evasion to access is out of scope — say that in the proposal. Escalating evasion converts a technical question into a legal one.
3. **Third-party public sites:** public data only, respect terms and `robots.txt`, identify the agent with a contact URL, rate-limit conservatively. Default posture is the client's own authenticated access to a portal they already pay for — that is where the $15–60k engagements live and it is legally clean.
4. **No credential in plaintext, ever.** Envelope encryption, per-client keys, never logged, never in a screenshot. Preference order: client-supplied session/cookie injection → a client-controlled credential broker → delegated access → stored credentials as a last resort with written acknowledgement. **Storing a TOTP seed defeats the second factor** and may breach the client's own policy even when they ask for it — raise it in the first call.
5. **Healed selectors do not write until validated.** Below threshold → quarantine, alert, do not write. Never guess into a client's system.
6. **Every screenshot and DOM snapshot is redacted before storage** and never leaves the client's boundary. A portal screenshot is `CLIENT_CONFIDENTIAL` and cannot reach a free-tier vision model, and cannot appear in a public heal-log demo.
7. **No write-back into a client portal in v1.** A wrong write into a system of record is unrecoverable in a way a wrong read is not. If we ever take it, it is priced and contracted separately.

Inherited from `00`: no hardcoded model names (R1), no secrets in the repo including fixtures, $0 recurring cost, permissive licences only.

## The resolution cascade

Cheapest and most reliable first:

1. **Cached selector** — verify it still matches exactly one element and that the element satisfies the target's assertions.
2. **Accessibility tree** — role + accessible name + relationship. The most semantically stable layer of a page and the one most automation ignores.
3. **Text anchors and structural relations** — "the value in the cell to the right of the label 'Claim Number'". Survives most restyling.
4. **Attribute and structural heuristics** — `data-testid`, stable id patterns, table header position, column index with header verification.
5. **Visual grounding via a model** — last resort. Redacted screenshot only, client opt-in only. Otherwise this rung is disabled and the task escalates to human review.

Rungs 1–4 must carry the overwhelming majority of resolutions. Instrument what proportion actually reaches rung 5 and publish it.

## The post-heal validation gate — mandatory before any write

- **Type and format assertions** from the task spec. A claim number matching `^[A-Z]{2}\d{8}$` is a far stronger signal than any model confidence.
- **Domain assertions** — range, enum membership, date plausibility, currency sanity.
- **Cross-field consistency** — does the row still foot? Do fields co-vary as they did historically?
- **Distributional check against history** — catches the resolver locking onto the *adjacent column*, the most common and most dangerous silent failure, where every individual value looks valid.
- **First-heal human confirmation** — the first heal to a new selector completes into quarantine; a human confirms before data is promoted. Subsequent heals to the same selector run automatically.
- **Quarantine, not failure.** A quarantined run keeps its data, evidence and diff, and offers one-click promote or discard. A client who sees the system refuse to write questionable data trusts it more than one that never refuses.

## Stack & layout

Python 3.12+, `uv`, `ruff`, `mypy --strict`. Playwright (containerised). Prefect 3 *or* Temporal (open decision). FastAPI. Next.js 15 + `bok-ui`. Postgres for run history, heal events and the selector cache. Parquet + DuckDB as the landing zone.

```
selector_drift/
  spec/         task spec schema, versioning, validation
  scope/        scope.yaml schema, authorisation gate   ← build first
  browser/      Playwright driver, containerised, session + MFA handling
  resolve/      cascade.py (the five rungs), cache.py, validate.py, quarantine.py
  extract/      typed extraction into Pydantic schemas
  quality/      schema drift, null rates, row counts, distribution shift
  land/         Parquet writer, DuckDB catalog, idempotent upsert
  orchestrate/  flows, scheduling, retries, quota accounting
  heallog/      heal events, evidence storage, redaction, diffing
  api/          FastAPI
testbench/      the mutating portal + mutation engine   ← shipped artifact
web/            Next.js 15 + bok-ui
```

**Storage rule:** DuckDB does not support concurrent writers across processes. Write per-run Parquet, union at read time, use DuckDB as the query layer over a partitioned lake, and compact to avoid the small-file problem. Do not architect around a shared DuckDB file written by parallel flows.

## The test bench — a shipped artifact, not a fixture

A deliberately mutating web application: realistic portal (login, search, results table, detail page, export) with a mutation engine applying parameterised UI changes on a schedule — class renames, DOM restructuring, label rewording, column reordering, framework markup changes, id churn, table-to-div conversion, pagination changes — plus **adversarial mutations designed to induce a plausible-but-wrong heal** (a decoy adjacent column with believable values).

It gives us a heal log with known ground truth, a regression suite, and a public artifact that makes the claims checkable. No competitor has one, because building it is unglamorous.

## The ninety-day log

**Start it on day one of week 13, before the product is finished, running whatever exists.** It takes calendar time that cannot be compressed, and it is the primary sales asset. Targets must be ethically and legally clean — our own deployed applications, the test bench, and any consenting client portal. A partial log with an honest start date beats no log.

Headline shape: *"90 days across 3 portals, 41 UI changes, zero manual selector fixes, 99.2% run success, 2 quarantined runs correctly refused."* The quarantine count belongs in the headline — it is proof the safety mechanism works.

## Watch for

- **Distribution-shift alerting will cry wolf.** Seasonality, month-end, holidays, genuine business change. Use PSI or a KS test with a documented threshold and a warm-up period, require persistence across runs before alerting, and let the client mark an alert as expected. An alerting system a client mutes is worse than none.
- **A heal you cannot reproduce is an anecdote.** Store enough to replay: DOM snapshot, redacted screenshot, cascade decisions, model call ids, response fingerprints.
- **Playwright in containers is fiddly.** Pin the browser version, one context per run, hard memory limits, reap zombies, and use real orchestrator persistence so a scheduled runner survives a host restart — not a `while true` loop.
- **Idempotency and late data.** Deterministic run key plus upsert on a natural key. Re-running a window must not duplicate rows; late records must not corrupt a closed partition.
- **Free-tier quota during a live run.** A scheduled 04:12 run hitting a daily cap looks like a product defect.
- **Frame it as a data pipeline, never as "scraping."** *"Production data pipeline with self-healing extraction and data-quality contracts."* The framing selects the buyer and prices the same work an order of magnitude apart.

## Testing bar

Mutation suite — every mutation class heals correctly, **and every adversarial mutation is refused by the validation gate; the second set matters more** · scope gate tests (unknown target refused, expired authorisation refused, rate limit enforced) · credential tests (no plaintext at rest, nothing in logs, redaction verified on screenshots and DOM snapshots) · quarantine tests (below-threshold never writes; promotion audited) · idempotency under deliberate replay · data-quality tests (synthetic shift detected, synthetic seasonality not falsely alerted after warm-up) · degraded mode (rungs 1–4 heal the majority of mutation classes with all providers disabled) · Playwright E2E with fixtures kept separate from the driver's own · axe zero serious/critical. Coverage floor 75%.

## Definition of done

Brief §12, ten items.

## Before planning

Brief §13 has 7 open decisions; `00` §12 has 7 more. Q1 (which three targets the ninety-day log runs against — needed **before** week 13 day one) and Q2 (credential posture) gate the build. Do not assume them — ask.
