# Selector Drift — the minimal `bok-ui` slice

**Status:** approved in brainstorming 2026-09-26 · **Parent:** [design](2024-11-07-selector-drift-design.md) §3.3
**Plan task:** new 34A (before Task 35) · **Built in:** `../backoffice-kit`, not in this repo
**Siblings:** [API contract](2026-09-26-selector-drift-api-contract.md) · [web UI](2026-09-26-selector-drift-web-ui.md) · [test bench](2026-09-26-selector-drift-testbench-portal.md)

---

## 1. Why this exists

`INTERFACE-CONTRACT.md` §8 schedules `bok-ui` v0.1 for the end of programme week 3 and the deferred
components this project needs (`RunTimeline`, `DiffView`, the DOM `EvidencePane` adapter) for week
13. Selector Drift now runs first (audit OQ1), so none of it exists when the UI phase starts.

Decided 2026-09-26: **the same ruling as `bok-core`.** Hand-build only what this project renders, in
`backoffice-kit`, with every prop signature taken from contract §6. Nothing is built in `web/` that a
later product would have to re-extract. Components ship with the contract's props, so `ledger-sense`
and `ground-truth` later inherit real components, not a rewrite.

---

## 2. Where it lives and how it is consumed

| | |
|---|---|
| Repo / branch | `backoffice-kit`, branch `bok-ui-minimal`, worktree `.worktrees/bok-ui-minimal` (mirrors `bok-core-minimal`) |
| Package | `packages/bok-ui`, name `bok-ui`, `"type": "module"`, React 19 peer dependency |
| Build | `tsup` → ESM + `.d.ts`; CSS emitted as `bok-ui/tokens.css` and `bok-ui/print.css`, importable without React |
| Fonts | Newsreader, Geist Sans, IBM Plex Mono, all OFL, **self-hosted `woff2` inside the package** — no Google Fonts CDN call (`00` §5.2) |
| Consumer | `selector-drift/web/package.json`: `"bok-ui": "file:<absolute path>/backoffice-kit/.worktrees/bok-ui-minimal/packages/bok-ui"`; `next.config.ts`: `transpilePackages: ['bok-ui']` |
| Path | **Absolute, not relative** — the same ruling as the `bok-core` path dependency (Task 1 ledger, Important #1): a relative path breaks when the worktree merges |
| Exit | when `bok-ui` publishes to npm, the `file:` specifier becomes a version range. One line; no application code changes |

---

## 3. Scope — what ships, and what does not

| Ships | Why this project needs it | Contract |
|---|---|---|
| `tokens.css` — warm-graphite ramp, four accents each with an `-ink` sibling, severity and provenance scales in the two-role split, 4px spacing, radius 6/2/0, focus ring | every screen | §6.1 |
| Density mechanism, `comfortable` + `compact`, driven by `data-density` on `<html>` | every screen | §6.1 |
| `print.css`, standalone | the heal log and quality screens print | §6.1 |
| `Claim`, `ProvenanceRailProvider`, `ProvenanceRail` | every extracted value | §6.2 |
| `EmptyState` (`scope.measuredNoun` required), `ErrorState`, `PartialFailureState`, `Skeleton` | the designed states on every screen | §6.2 |
| `SeverityBadge` | quality findings | §6.2 |
| `RunTimeline` | run timeline screen | §6.3 → **early** |
| `DiffView`, word-level | heal log | §6.3 → **early** |
| `EvidencePane` + `DomAdapter` + `TextAdapter` | heal log before/after | §6.2 (text) + §6.3 DOM → **early** |

**Deliberately not shipped:** `FindingsTable`, `ConfidenceMeter`, chart primitives, the roving-list
keyboard primitive, `dense` density, `QuotaStrip`. Nothing in this project's v1 renders them.
`ConfidenceMeter` is absent **on purpose**: this product has no confidence score (design §5.4), and
the web UI spec carries a test that no confidence element renders anywhere.

---

## 4. Component contracts

Props are the contract's where the contract defines them. Where it does not (the three early
components), the props below are proposed as the contract text — see §6.

### 4.1 `Claim` and the rail

```tsx
<ProvenanceRailProvider hrefForSnapshot={(id) => string}>   {/* exactly one per app; owns the single 380px panel */}
  <Claim payload={rail /* RailPayload from to_rail() */}>{display}</Claim>
</ProvenanceRailProvider>
```

- `Claim` renders `display` with a 1px dotted underline in the provenance-state `-ink` colour, a
  glyph, and a visually hidden state label ("verified", "modelled", "unverified"). Never colour alone.
- Activating it (click, `Enter`, or `Cmd/Ctrl+.` while focused) asks the provider to open the rail
  with that payload. **A `Claim` never renders a rail itself** (contract §6.2: one rail, not one per
  claim).
- The trigger carries `aria-expanded` and `aria-controls`. Opening moves focus into the rail's
  heading; `Escape` or the close button returns focus to the originating `Claim`.
- The rail renders the chain top to bottom — value, method glyph + label, source, confidence **only if
  present** (for `DETERMINISTIC` it is absent, not "n/a"), timestamp, inherited caveat verbatim.
  Parents nest.
- `DomSource` renders as `rung N · snapshot <short id> · <selector>`, the snapshot id linking through
  the provider's `hrefForSnapshot(id)`. `bok-ui` knows nothing about heal URLs.
- Motion: slide in 180 ms, chain items stagger 30 ms; all of it off under `prefers-reduced-motion`.
- **Below 420px the rail is full width** with a visible close button (contract §6.2 — 380px does not
  fit a 375px viewport).

### 4.2 States

```tsx
<EmptyState scope={{ noun: "quarantined runs", measuredNoun: "runs",
                     measured: 41, skipped: 0 }} />
// -> "Nothing quarantined. 41 runs passed validation." Never a celebration, never an illustration.
<ErrorState title="…" detail="…" retry={() => …} />
<PartialFailureState succeeded={[…]} failed={[{ what: "before screenshot", why: "evidence corrupted (hash mismatch)" }]} />
<Skeleton shape="table" rows={8} />
```

### 4.3 `RunTimeline` (early)

```tsx
type RunState = "completed" | "healed" | "quarantined" | "failed" | "refused";
<RunTimeline
  runs={Array<{ id: string; at: string /* ISO */; state: RunState; label: string }>}
  range={{ from: string; to: string }}
  hrefFor={(id) => string}
/>
```

- A horizontal track per call, one chip per run. Each state has a token colour, **a glyph and a text
  label** — `completed` ✓, `healed` ↻ (drift indigo, the only use of the accent), `quarantined` ⏸,
  `failed` ✕, `refused` ⊘.
- Chips are links, reachable by `Tab`; arrow keys move between chips; the track is an `ol` with an
  accessible name.
- Days with no run render an empty tick, so a missed run is visible as a gap rather than silently
  absent (design §9.2: missed runs are published, not hidden).
- Below 640px the track wraps into a vertical list; there is no horizontal page scroll at 375px.

### 4.4 `DiffView` (early, word-level)

```tsx
<DiffView spans={Array<{ kind: "add" | "remove" | "same"; text: string }>} />
```

- Renders spans inline in Plex Mono. Additions and removals carry `ins`/`del` semantics **and** a
  `+`/`−` glyph, not colour alone.
- Tokenisation is the producer's job (the API's `dom_diff`), not the component's. This keeps
  `DiffView` generic across the three consumers the contract names.

### 4.5 `EvidencePane` + `DomAdapter` (early)

```tsx
<EvidencePane
  adapter={DomAdapter}
  source={{ screenshotUrl: string; alt: string; viewport: [number, number]; html?: string }}
  ranges={Array<{ id: string; label: string; bbox: { x: number; y: number; width: number; height: number } }>}
  outlineState={"idle" | "drawing" | "drawn"}
/>
```

- Renders the screenshot scaled to its container, with an SVG overlay drawing each range's `bbox`
  (scaled from `viewport`) as an outline plus a text label. The outline stroke uses `--bok-accent-selector-drift-ink`.
- `outlineState="drawing"` animates the stroke with `stroke-dashoffset` over
  `var(--bok-evidence-outline-duration, 220ms)`. **The sequence — before then after — belongs to the
  consumer**, which flips `outlineState` on each pane in turn. Under `prefers-reduced-motion` the
  duration is `0s` and `drawing` renders as `drawn`.
- `html`, when given, renders in a collapsible panel **as escaped text**, never as markup. The
  adapter has no `dangerouslySetInnerHTML` path. The HTML is a third-party page.
- `source.alt` is required and is supplied by the consumer.
- `TextAdapter` (contract §6.2) ships alongside so the adapter interface is proven by two
  implementations, not one.

---

## 5. Tests (in `backoffice-kit`)

- **Vitest + Testing Library + `axe-core`** for every component in §3: zero serious or critical
  violations, rendered in light and dark, in both densities.
- `scripts/check-contrast.mjs` measures every ratio in `tokens.css` (4.5:1 for `-ink`, 3:1 for base),
  running before the axe job, as contract §6.1 requires.
- Behaviour tests:
  - `one rail: opening a second Claim replaces the payload, never adds a panel`
  - `focus returns to the originating Claim on Escape`
  - `a DETERMINISTIC claim renders no confidence row`
  - `the rail is full width below 420px`
  - `every RunTimeline chip carries a text label`
  - `a missed day renders as a visible gap`
  - `DiffView marks additions and removals without colour alone`
  - `DomAdapter never renders html as markup`
  - `the outline duration is 0s under reduced motion`
  - `EmptyState refuses to render without measuredNoun` (a type error and a runtime assertion)
- **No Storybook in this slice.** It belongs to `backoffice-kit`'s own definition of done (`00` §10.3)
  and costs a day this project does not need.

---

## 6. The contract change this slice implies — flagged, not applied

Three §6.3 components land at build week 7 of this project instead of programme week 13, and their
props become precedent for two later consumers. The plan task **drafts** the following and stops for
sign-off, because `INTERFACE-CONTRACT.md` belongs to `backoffice-kit`:

- §6.3 table: `RunTimeline`, `DiffView`, `EvidencePane` DOM adapter → "Available: `bok-ui-minimal`,
  selector-drift build week 7".
- §6.2: add the §4.3–§4.5 prop signatures above.
- §9 changelog entry naming the reason (programme order changed, audit OQ1).
- Alongside, from the API contract §6.2: the proposed `QuerySource` variant.

Until it is signed off, the slice still ships. Pinning the props in *this* spec is what keeps them from
drifting in the meantime.
