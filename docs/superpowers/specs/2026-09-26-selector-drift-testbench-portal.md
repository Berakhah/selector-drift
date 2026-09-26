# Selector Drift — the test-bench portal

**Status:** approved in brainstorming 2026-09-26 · **Parent:** [design](2024-11-07-selector-drift-design.md) §7
**Plan tasks:** 7, 8, 12 (rewritten); 9–11A (unchanged engine, one data amendment)
**Siblings:** [API contract](2026-09-26-selector-drift-api-contract.md) · [web UI](2026-09-26-selector-drift-web-ui.md) · [`bok-ui` slice](2026-09-26-selector-drift-bok-ui-slice.md)

The bench is a shipped artifact, not a fixture: it is the treatment arm of the ninety-day log and the
thing a buyer is handed to check the claims. Its one non-negotiable property is **the markup a
browser sees is exactly the markup the tested engine produced.** Everything below serves that.

---

## 1. Decision: a server-rendered Python portal, not Next.js

Decided 2026-09-26. **This reverses the architecture the audit's C7 fix chose, because that fix does not
work either.**

Plan Task 8 (as revised by the audit) had `middleware.ts` POST *"the rendered HTML"* to a Python
mutator. Two independent defects:

1. **Next.js middleware runs before rendering.** It sees the request, not the rendered response body,
   and cannot rewrite a page's HTML. Reversing the proxy — the mutator in front, fetching from Next —
   fixes that and hits the second.
2. **Hydration undoes the mutation.** Next ships client JavaScript that hydrates the server HTML. The
   mutated markup no longer matches what React rendered; React 19 recovers from a hydration mismatch
   by re-rendering on the client. **The browser — and therefore our Playwright resolver — sees the
   unmutated page**, while every served-markup test over plain HTTP passes. That is C7's failure
   again, quieter: the heal log would record heals scored against a page the browser never showed.

Stripping Next's scripts in the proxy would work, but leaves two runtimes, a proxy, a portal that is
Next.js in name only, and a silent break on any Next upgrade that changes script injection.

**Adopted:** FastAPI + Jinja2 renders the portal, and every HTML response passes through
`apply_all(mutations_for(day, "testbench"), html)` **in the same process**. One language, one engine,
no hydration, and *served equals tested* by construction. It is also closer to the buyer's world:
the portals behind $15–60k integrations are server-rendered legacy applications (ASP.NET WebForms,
JSP, PHP), not React SPAs. Framework-shaped markup churn is still exercised by the `FrameworkMarkup`
mutation class. Audit L8 (a Next app becoming a Python package under grimp) disappears with it.

---

## 2. Layout

```
testbench/
  portal/
    app.py            FastAPI app factory; session middleware; the mutation response hook
    routes.py         /login /logout /search /results /claims/{claim_number} /export.csv /healthz
    data.py           the synthetic dataset and its daily delta (§4)
    templates/        base.html login.html search.html results.html detail.html
    static/           portal.css  (no JavaScript is shipped by the base portal)
  mutations/          Tasks 9-11A: one module per class, Mutation.apply(html) -> html, lxml
  schedule.py         mutations_for(day, target) -> list[Mutation]   (seed = sha256(f"{day}:{target}"))
  answers/            expected node per (day, field) — importable by tests and score.py only
  score.py            reads the product's tables, judges each bench run, writes audit_log "scored"
  Dockerfile
  README.md           DoD 5: every mutation class, the seeding, how to reproduce a day's DOM
```

Import contracts (added to `.importlinter`):

| Contract | Why |
|---|---|
| `selector_drift/*` must not import `testbench.*` | extends the answer-key rule to the whole bench: the product must not know it is being tested |
| `testbench.portal`, `testbench.mutations`, `testbench.schedule` must not import `selector_drift` | the bench stands alone and can be lifted into its own repo |
| only `testbench.score` may import `selector_drift.db` | the scorer is the one bridge, and it is read-only against the product's tables |

---

## 3. The request path

```
GET /results?page=2
  -> session check (redirect to /login if absent)
  -> route renders Jinja template from data.py for today's dataset
  -> response hook: day = request_day(request)
                    html = apply_all(mutations_for(day, "testbench"), html)
                    inject <meta name="sd-mutation-seed" content="<seed>;<mutation names>">
  -> 200 text/html
```

- **Every** `text/html` response goes through the hook, including login and error pages. Non-HTML
  responses (`/export.csv`, `/healthz`, static CSS) are not mutated.
- `request_day(request)` is the **UTC** date of the request (audit M20: local time would shift the
  seed by a day at the boundary), unless a valid override is present (§3.1).
- **The product never sends an override.** The runner requests the page like any client and records
  what it saw; the scorer reads the seed from the stored snapshot's `<meta>` tag, not from the run's
  window date. A run that crosses midnight is therefore scored against the page it actually saw, and
  `selector_drift/` contains no bench-specific code path.

### 3.1 Date override — for verification only

`X-Testbench-Date: 2026-09-10` is honoured only with
`X-Testbench-Signature: hex(hmac_sha256(TESTBENCH_OVERRIDE_SECRET, "2026-09-10"))`. The secret lives
in the VM's `.env`, never in the repo. Without a valid signature the header is ignored, not rejected,
so a stranger cannot steer the public bench's schedule or probe for the secret. This replaces the
first draft's environment-variable override, which `curl` never forwarded (audit M27).

### 3.2 Pages

| Route | Contents | Notes for the resolver |
|---|---|---|
| `/login` | username, password, submit | the public demo account `demo` / `demo`, printed in the README — not a secret, it exercises the credential path |
| `/search` | claim number, claimant, status, filed-from/to; `GET` form to `/results` | a navigation step in the task spec |
| `/results` | the results table, 50 rows per page, numbered pagination links | the extraction target |
| `/claims/{claim_number}` | a detail page: a label/value definition list | the "value to the right of the label" case for rung 3 |
| `/export.csv` | the current result set as CSV | the brief's "export" page; not a resolution target in v1 |

Unauthenticated requests to anything but `/login` and `/healthz` get `302 /login`. Sessions use
Starlette's signed-cookie `SessionMiddleware` with a secret from `.env`.

**The base portal ships no JavaScript.** The only script that can appear on a bench page is one a
mutation injects (§5, `VirtualisedRows`). A mutation's effect is therefore always the engine's, never
a framework's.

---

## 4. The synthetic dataset

**Amendment to plan Task 7:** the results table has **six** columns, not five.

| Column | Format | Why it exists |
|---|---|---|
| `Claim Number` | `^[A-Z]{2}\d{8}$` | the natural key; format assertion |
| `Claimant` | synthetic person name | exercises redaction of names |
| `Amount` | currency, 2 dp, `100.00`–`250,000.00` | the field the decoy attacks |
| `Reserve` | currency, 2 dp, same range and shape, **no arithmetic relation to Amount** | the second same-format column `ValueSwap` needs (§5); the absence of a relation is deliberate, so `CrossFieldAssertion` cannot catch the swap and only `RowKeyContinuity` does, as Task 11A claims |
| `Status` | enum `Open` / `Pending` / `Paid` / `Closed` | domain assertion |
| `Filed` | ISO date within the last 3 years | date plausibility |

- **Base set:** 500 claims generated from a fixed seed (`0`). Names from a fixed list of obviously
  synthetic names; no real person's data at any point (sensitivity `SYNTHETIC` is true by construction).
- **Daily delta, deterministic from the date:** about 2% of claims change `Amount` or `Status`, about
  5 new claims are filed, about 3 close. A real portal is not static, and the delta keeps
  `RowKeyContinuity` honest: on a benign day ~98% of keys agree, above its 0.95 threshold; after a
  decoy or swap, agreement collapses.
- The delta is a pure function of `(day)`; `data.py` can materialise any day's dataset, so any day's
  page is reproducible from the date alone.

---

## 5. Mutation classes — unchanged, with two notes

The eight benign classes (Tasks 9–10) and the seven adversarial classes (Tasks 11–11A) are unchanged in
name and purpose. Two clarifications follow from this spec:

- **`ValueSwap`** exchanges `Amount` and `Reserve` values row-wise with markup untouched (§4).
- **`VirtualisedRows`** is the one mutation that injects a script: an inline script that removes all
  but the first 10 rows from the DOM on `DOMContentLoaded`, as a virtualised grid would. It is part of
  the engine's output, so the rule holds; it is also the case that proves why §6's test compares the
  browser's DOM rather than the HTTP body — the two differ for exactly this class.

---

## 6. Tests

### 6.1 The one that guards the whole bench

```python
# tests/testbench/test_served_equals_tested.py
@pytest.mark.e2e
@pytest.mark.parametrize("day", FIXED_DAYS)          # includes a day scheduling VirtualisedRows
def test_the_page_a_browser_sees_is_what_the_engine_produced(bench_server, browser, day):
    page = logged_in_page(browser, bench_server, override_day=day)   # signed override
    page.goto(f"{bench_server}/results")
    seen = normalise(page.evaluate("document.documentElement.outerHTML"))
    expected = normalise(render_in_browser(browser,                  # same engine, rendered locally
                   apply_all(mutations_for(day, "testbench"), render_results(day))))
    assert seen == expected
```

It compares **the DOM after load in a real browser**, not the HTTP body. The first draft's test
compared bodies, which is exactly the comparison hydration defeats. Both sides go through a browser so
that `VirtualisedRows`' script runs on both.

### 6.2 Rewritten Task 7 E2E (pytest-playwright, Python)

- `test_login_is_required_for_results`
- `test_results_table_has_six_columns_of_synthetic_claims` — header count 6, first cell matches the
  claim-number pattern
- `test_pagination_serves_fifty_rows_per_page`
- `test_detail_page_labels_every_value`
- `test_export_csv_matches_the_results_page`

### 6.3 Engine, schedule and override

- `test_schedule_is_deterministic_for_a_day`, `test_different_days_differ` (kept from Task 8)
- `test_every_html_response_carries_the_seed_meta_tag`
- `test_csv_and_static_responses_are_never_mutated`
- `test_the_day_is_utc_not_local` — fake clock at 23:30 in UTC−5
- `test_an_unsigned_date_override_is_ignored`, `test_a_signed_date_override_is_honoured`
- `test_the_dataset_for_a_day_is_reproducible`
- `test_benign_daily_delta_keeps_key_agreement_above_threshold` — ≥ 0.95 across 90 consecutive days
- `test_value_swap_is_invisible_to_cross_field_checks` — proves §4's no-relation property holds
- `test_the_bench_ships_no_javascript_of_its_own` — scans templates and static
- `test_the_runner_identifies_itself_to_the_bench` — the bench's access log records the product's
  `User-Agent` with its contact URL (brief §3.3)

---

## 7. Deployment (Task 12)

- One container, `python:3.12-slim`, **built for `arm64`** (the free Oracle shape, design §9.2),
  `uvicorn testbench.portal.app:create_app --factory`, non-root, read-only rootfs, tmpfs `/tmp`,
  `cap_drop: [ALL]`, `no-new-privileges`.
- On the P-4 VM behind the same reverse proxy (Caddy) as the API, at a stable HTTPS URL. Secrets
  `TESTBENCH_SESSION_SECRET` and `TESTBENCH_OVERRIDE_SECRET` in the VM's `.env`.
- The bench's scope record lists exactly its one origin, so the browser's origin allowlist (Task 4)
  names a single host.
- Task 12 Step 2's verification keeps its shape and now works:

```bash
curl -s -b demo.cookies "$BENCH_URL/results" | sha256sum
curl -s -b demo.cookies -H "X-Testbench-Date: 2026-09-10" \
     -H "X-Testbench-Signature: $(printf 2026-09-10 | openssl dgst -sha256 -hmac "$TESTBENCH_OVERRIDE_SECRET" -r | cut -d' ' -f1)" \
     "$BENCH_URL/results" | sha256sum
```

Expected: two different hashes.
