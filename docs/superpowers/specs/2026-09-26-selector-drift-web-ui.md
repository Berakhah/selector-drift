# Selector Drift — web UI

**Status:** approved in brainstorming 2026-09-26 · **Parent:** [design](2024-11-07-selector-drift-design.md) §10
**Plan tasks:** 35, 36, 37, 38, 39, 39A (rewritten) · **Consumes:** [API contract](2026-09-26-selector-drift-api-contract.md), [`bok-ui` slice](2026-09-26-selector-drift-bok-ui-slice.md)
**Sibling:** [test bench](2026-09-26-selector-drift-testbench-portal.md)

Design §10's scope call stands: screens 1, 3, 4 and 6 are built in full; screen 2 (spec editor) is
read-only YAML plus validation; screen 5 (data quality) is a table with thresholds and reasons, with
no plotted distributions. This spec adds what §10 left unstated: how the app is built, the states on
every screen, and the fixtures its tests run against.

---

## 1. Architecture

| Concern | Decision | Why |
|---|---|---|
| Framework | Next.js 15, App Router, TypeScript strict | `00` §3.6 |
| Rendering | **Server components by default.** Client components only for: the provenance rail, the command palette, the promote/discard dialog, the density toggle, the outline animation | Nothing else on these screens is interactive; server rendering keeps evidence off the client until it is shown |
| Data | Server components call `http://api:8000/api/v1` with `openapi-fetch`, typed by the generated `web/lib/api.d.ts` (API contract §8), forwarding the incoming `sd_session` cookie from `cookies()` | No hand-written response types; a backend change the UI has not absorbed fails CI |
| Origin | `next.config.ts` rewrites `/api/v1/:path*` → `http://api:8000/api/v1/:path*` | Browser, cookie and API share one origin: `SameSite=Strict` works and the API keeps CORS closed |
| Auth | `middleware.ts` redirects to `/login?next=…` when `sd_session` is absent; a `401` from any server fetch does the same. `/login` is a plain form posting to `/api/v1/auth/login` | The cookie's validity is checked by the API, not guessed by the middleware |
| Mutations | Client component `fetch('/api/v1/quarantine/…', { method: 'POST' })` then `router.refresh()` | Server components re-fetch; no client cache to invalidate |
| Styling | `bok-ui/tokens.css` + CSS modules. Tailwind v4 `@theme` reads the same custom properties | `00` §5.2 |
| Density | `sd_density` cookie (`comfortable` \| `compact`, default `compact`) read in the root layout and set as `data-density` on `<html>` | Server renders the right density; no flash |
| Print | `import 'bok-ui/print.css'` in the root layout, plus `web/app/print.css` for this product's two report views | The heal log is forwarded to auditors (`00` §5.1) |

`next` is only used for `?next=` values that are same-origin relative paths; anything else redirects
to `/runs`. An open redirect on the login page of a product that holds client portal evidence would
be an own goal.

```
web/
  app/
    layout.tsx            shell, nav, RailProvider, palette, density
    login/page.tsx
    targets/page.tsx
    runs/page.tsx         runs/[run_key]/page.tsx
    heals/[id]/page.tsx
    quarantine/page.tsx
    quality/page.tsx
    specs/[target]/page.tsx
    (each route) loading.tsx, error.tsx
    print.css
  components/
    ClaimValue.tsx  ScopeCard.tsx  VerdictTable.tsx  HealHeader.tsx  BeforeAfter.tsx
    DecisionDialog.tsx  RemediationPanel.tsx  CommandPalette.tsx  DensityToggle.tsx  StatFigure.tsx
  lib/
    api.ts  api.d.ts  openapi.json  format.ts
  middleware.ts
  e2e/  (see §6)
```

---

## 2. Visual rules that apply everywhere

- Light-first editorial instrument (`00` §5.1). **Drift indigo is used for the heal state and nothing
  else** — not links, not buttons, not focus rings (those use `--bok-accent-ink`).
- Run state, severity and verdict outcome are **never colour alone**: token colour + glyph + text
  label, on every screen, including the timeline chips and the verdict table.
- Tabular numerals (`font-variant-numeric: tabular-nums`) on every figure.
- Every extracted value is a `<ClaimValue>` (a thin wrapper over `bok-ui`'s `Claim` that formats the
  value and passes the `RailPayload` through). Every aggregate is a `<StatFigure>` — a figure plus a
  "how this is computed" link to its `method_url` (API contract §6.2). No figure is rendered bare.
- **No confidence element exists anywhere.** Design §5.4: there is no score in this product, and the UI
  must not imply one is hidden.

---

## 3. Screens

### 3.1 `/targets` — screen 1 (full)

One `ScopeCard` per `TargetView`, ordered active → expired → unscoped.

- Card shows: target, basis, **derived** sensitivity (labelled "derived from basis — not editable"),
  authorising party, authorised and expiry dates with days remaining, rate limit as
  "1 request / 30 s", origins, first-heal policy, retention, legal review state, and the first 12
  characters of `record_sha256` with a copy button.
- `expired` and `unscoped` cards render **locked**: `aria-disabled="true"`, a lock glyph, and
  `locked_reason` in words ("No authorisation record", "Authorisation expired on 2027-01-04"). Links
  from a locked card go to the target's run history only.
- An expiry within 30 days renders a `medium` notice on the card — the one thing on this screen that
  becomes urgent on its own.

### 3.2 `/runs` and `/runs/[run_key]` — screen 3 (full)

- `/runs`: one `RunTimeline` per target over the selected range (default: the last 30 days, UTC),
  filters `target` and `from`/`to` in the query string so a view is linkable. Above the timelines, a
  `StatFigure` row from `GET /stats`: elapsed days, run success rate, missed runs, heals,
  quarantines, manual interventions, rung shares.
- `/runs/[run_key]`: header (target, window, state + glyph + label, quarantine resolution if any);
  attempts table (every attempt, including failed ones — audit M13); per-field resolutions table
  (field, winning rung, each rung tried with its outcome and ms, link to the heal if one fired); a
  link to quality findings for the run.

### 3.3 `/heals/[id]` — screen 4, the signature screen (full)

Top to bottom:

1. **`HealHeader`** — `HealView.summary` as the headline sentence (design §5.4: *"Selector broke
   04:12. Healed to … via rung 3 … Independently confirmed by rung 4 …"*), the decision badge, and the
   replay status in words.
2. **`BeforeAfter`** — two `EvidencePane`s (`DomAdapter`) side by side, stacked below 900px. Each
   outlines the healed field's `bbox`. The one orchestrated motion in the product: on mount, the
   "before" outline draws over 220 ms, then the "after" outline draws over 220 ms. Disabled under
   `prefers-reduced-motion`. A caption under each pane states the capture time and "outline recorded
   at capture, not recomputed" (API contract §7.2).
3. **`DiffView`** of `GET /heals/{id}/diff`.
4. **Cascade trace** — each rung tried with family, outcome and ms; the winner marked; the confirmer
   named as `rung N (family)` or `VALUE (row-key continuity)`, or "no cross-family confirmer resolved".
5. **`VerdictTable`** — one row per `Verdict`: check, outcome (glyph + label), observed, threshold,
   reason. Rows with outcome other than `PASS` sort first.
6. **Sample values** — the first ten healed values as `ClaimValue`s; each opens the rail showing
   `DETERMINISTIC`, `rung N · snapshot … · selector`.
7. **Decision** — resolved as, actor, timestamp, reason; or, if the run is still quarantined, a link
   to it on `/quarantine`.

Partial states on this screen (§5): `before.available=false` (`not_captured`, `corrupted`,
`expired`) renders a `PartialFailureState` in place of that pane while the rest of the screen still
renders; `replay_status="not_reproducible"` renders a `high` notice at the top — *"This heal did not
reproduce from stored evidence. It is counted as non-reproducible in the published log."*

### 3.4 `/quarantine` — screen 6 (full)

- One card per `QuarantineView`, newest first: reason, the `failing` verdicts in a `VerdictTable`, a
  20-row preview as `ClaimValue`s, links to each heal.
- **`RemediationPanel`** (audit M11): for each failing field, the spec excerpt and a link to
  `docs/runbook-spec-update.md` — *"If the portal changed permanently: edit this field's description,
  bump `spec_version`, re-run, confirm the first heal."* This is the retainer's daily work, and the
  quarantine screen is where the operator is when they need it.
- **Promote / Discard** open `DecisionDialog`: a required reason (1–500 chars) and a summary of what
  promotion will do (*"Re-runs every check with your confirmation. Writes only if they all pass."*).
  A `409 still-invalid` keeps the dialog open and lists the verdicts that still fail; it is not an
  error toast.
- Empty: `EmptyState` → *"Nothing quarantined. 41 runs passed validation."* — the count from
  `GET /stats`, never a celebration.
- `?resolved=true` shows history: every decision with actor and reason.

### 3.5 `/quality` — screen 5 (v1: table)

Table of `FindingView`s: severity (`SeverityBadge`), check, observed, **threshold**, **threshold
reason**, run link. Filter by target and run. No charts in v1 (design §10). Prints cleanly.

### 3.6 `/specs/[target]` — screen 2 (v1: read-only)

The YAML in a `<pre>` (Plex Mono, line numbers, no editor), and the validation verdicts beneath.
A banner states *"Read-only in v1. Specs are edited in the repository; see the runbook."*

### 3.7 `/login`

Username, password, submit. Errors in words (*"Username or password is incorrect"*, *"Too many
attempts — try again in 12 minutes"* from `Retry-After`). No "remember me".

---

## 4. Cross-cutting requirements from `00` §5 (Task 39A)

| Requirement | Implementation |
|---|---|
| Provenance rail on every extracted value | `ClaimValue` everywhere; one `ProvenanceRailProvider` in the root layout |
| `cmdk` command palette | `Cmd/Ctrl+K`. Entries: every screen in the nav (targets, runs, quarantine, quality, specs), each target's runs, "Promote this run" / "Discard this run" when a quarantined run is on screen (they open the same `DecisionDialog`), "Toggle density", "Print this report" on the two report views |
| Keyboard | every flow operable without a mouse; visible focus rings; `Escape` closes the rail, the palette and the dialog; the dialog traps focus and returns it |
| 375px | no horizontal page scroll on any route; before/after stacks; tables become labelled row cards below 640px |
| Density | `DensityToggle` in the shell, persisted to the `sd_density` cookie |
| Print | the heal log and quality screens print without the nav, palette, rail or dialog; the verdict table and both screenshots print; URLs of evidence links print after the link text |
| Reduced motion | the rail slide, chain stagger, and outline draw all go to `0s` |

---

## 5. Designed states — every route has all four

| State | Component | Trigger |
|---|---|---|
| Loading | `Skeleton` in `loading.tsx`, shaped like the page's content (never a spinner) | server fetch in flight |
| Error | `ErrorState` in `error.tsx`, with the problem `title` and `detail` and a retry | a non-2xx other than 401 |
| Empty | `EmptyState` **with a denominator**: `measuredNoun` + `measured` | zero rows |
| Partial | `PartialFailureState`: what loaded, what did not, and why | a heal with an unavailable evidence side; a run whose quality findings failed to load while the run did |

---

## 6. E2E fixtures — kept separate from the driver's own

Brief §11: a browser automation product tested with browser automation keeps its fixtures separate.
`selector_drift/browser/`'s own test fixtures (Task 4's local static server) are not used here.

```
web/e2e/
  fixtures/
    seed.sql                  deterministic rows for every designed state
    bundles/                  two synthetic evidence bundles + manifests (bench markup only)
    corrupt-bundle/           a bundle whose after.png fails its manifest hash
  global-setup.ts             starts postgres + the API (SD_FIXTURE_MODE=1), loads seed.sql,
                              creates operator "e2e" with a random per-run password,
                              writes fixture-ids.json and storageState.json
  fixture-ids.json            generated, gitignored
```

The seed provides at least one of each: completed run, healed-and-accepted run, quarantined run
(unresolved), promoted run, discarded run, failed run with two attempts, refused run, a heal whose
replay is `not_reproducible`, a heal with a `corrupted` bundle, a heal with `before` `not_captured`,
an unscoped target, an expired target, a target expiring within 30 days, and quality findings at
three severities.

**Tests read ids from `fixture-ids.json`.** The first revision hardcoded `/heals/1` and
`/heals/non-reproducible` — the second is a non-numeric id on an integer route and could only ever
`422`.

`SD_FIXTURE_MODE=1` only changes where the API reads evidence from (`web/e2e/fixtures/bundles`) and
refuses to start if `SD_ENV=production`. It never disables authentication.

---

## 7. Tests (Tasks 35–39A)

Kept from the plan, with ids from the fixture file: the targets tests, the timeline label test,
the run-detail click-through, the four heal-log tests, the three quarantine tests, the quality
threshold test, axe, the bounded keyboard test, provenance, print, and 375px.

Added:

| Test | Guards |
|---|---|
| `an unauthenticated visit redirects to login and returns after` | §1 auth |
| `a login next= pointing off-origin redirects to /runs` | §1 open redirect |
| `an expired target renders locked with its expiry date` | §3.1 |
| `a failed run shows every attempt, not only the last` | audit M13 |
| `a missed day is visible on the timeline` | design §9.2 |
| `the heal screen renders a partial state for a corrupted before pane and still shows verdicts` | §5 |
| `a first heal says the before capture does not exist` | API §7.2 |
| `promote that still fails lists the failing verdicts and keeps the dialog open` | API §5 |
| `the command palette can promote a quarantined run` | §4 |
| `no element on any route is labelled confidence` | design §5.4 |
| `drift indigo appears only on heal-state elements` | §2 — computed-style scan of every route |
| `every run state, severity and verdict outcome has a text label` | §2 |
| `axe: zero serious or critical on all seven routes plus /login, in both densities` | `00` §5.5 |
| `print: heal log and quality hide chrome and keep verdicts and screenshots` | §4 |
| `375px: no horizontal scroll on any route, including with the rail open` | §4 |

Coverage for `web/` is measured by the E2E run plus component tests for `ClaimValue`, `VerdictTable`,
`DecisionDialog` and `StatFigure` (Vitest).
