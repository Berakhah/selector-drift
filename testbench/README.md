# The test bench

A deliberately mutating claims portal -- a shipped artifact, not a test fixture.

## What this is

The treatment arm of the ninety-day log and the artifact a buyer checks the claims against:
a realistic claims portal (login, search, results table, detail page, export), rebuilt on
every request from a mutation schedule that changes on a predictable, reproducible calendar.
It is server-rendered FastAPI + Jinja2 and ships no JavaScript of its own -- the one exception
is `VirtualisedRows`, the single adversarial mutation that injects a script, documented below.

## The demo account

`demo` / `demo`. It is public on purpose: the bench has nothing in it worth protecting, and
the account exists only to exercise the credential path end to end (login form, session
cookie, authenticated navigation), not to gate access.

## Pages

| Path | What it does |
|---|---|
| `/login` | the credential form; sets the session cookie |
| `/search` | the search form |
| `/results` | the results table, 50 rows per page |
| `/claims/{claim_number}` | the detail page for one claim |
| `/export.csv` | the current result set as CSV |
| `/healthz` | liveness probe; unauthenticated, unmutated |

## The dataset

Six columns -- `Claim Number`, `Claimant`, `Amount`, `Reserve`, `Status`, `Filed` -- 500 claims
generated deterministically from seed `0` on the dataset's epoch, 2026-01-01. Each day applies a
daily delta on top of that base, seeded from the date alone: about 2% of the live (not closed)
claims change `Amount` or `Status` (the rate was taken over all claims until Task 26B, which made
~40% of live claims -- most of the results page -- move each day), about 3 close, about 5 new claims are filed, and claims older than three years are
archived out of the live set. `Reserve` is drawn independently of `Amount` -- deliberately: a
reserve is a claims adjuster's own estimate, not a function of the claimed amount, so the two
columns have no arithmetic relation for a cross-field assertion to check. That is also why the
bench's `ValueSwap` mutation (below) can be caught only by row-key continuity, not by footing.

## Mutation classes

Benign classes reshape markup without changing meaning; every one of them must still heal
correctly. Adversarial classes are built to produce plausible-but-wrong values and must be
refused by the post-heal validation gate -- the second set matters more than the first.

### Benign

| Class | What it does |
|---|---|
| `ClassRename` | CSS class names change; structure and text do not |
| `DomRestructure` | wrapper elements are added, removed or reordered around the same content |
| `ColumnReorder` | the results table's columns change order |
| `TableToDiv` | the results table becomes a `div`-based grid with ARIA roles |
| `LabelReword` | column headers and labels are reworded to a synonym |
| `IdChurn` | `id` attributes change value on every mutated day |
| `FrameworkMarkup` | markup takes on the shape a popular frontend framework would emit |
| `Pagination` | page size or pagination markup changes |

### Adversarial

| Class | What it does | The control that must refuse it |
|---|---|---|
| `DecoyColumn` | a "Prior Amount" column, styled identically to `Amount`, appears immediately to its left, so a cached selector or a position-based heal lands on the decoy | the post-heal validation gate's domain and distributional checks; this is the mutation the gate exists to refuse |
| `HiddenDecoy` | an invisible grid of well-formed claim numbers, labelled "Claim Number", precedes the real table in document order | rung 1-4's `visible()` filter, which only a resolver that checks visibility (not just markup) applies |
| `DuplicateLabel` | a second "Claim Number" column, carrying legacy references in the same format, appears before the real one | a resolver that refuses "first match" when more than one candidate satisfies the label anchor |
| `ValueSwap` | every row's `Amount` and `Reserve` values trade places; the markup does not move | `RowKeyContinuity` -- the same natural key, a different value, on nearly every row -- since `amount`/`reserve` have no cross-field relation to foot against |
| `LocaleFlip` | currency values switch to German grouping (`1,234.56` becomes `1.234,56`) | the `DomainAssertion` range check and the strict `FormatAssertion` pattern, which a flipped value fails |
| `VirtualisedRows` | the server still sends 50 rows, but an injected script trims the live DOM to 10 after load | the row-count contract, which only a resolver reading the live browser DOM (not the HTTP body) can apply |
| `ImageHeader` | the `Amount` header is rendered as `<th><img alt="Amount"></th>`, with empty `textContent` | rung 4's header verification, which must read the accessible name, not `textContent` |

## Seeding

Each day's schedule is seeded from `seed = sha256(f"{day}:testbench")`, where `day` is the
request's **UTC** date (never local time). One or two benign classes are drawn every day; about
one day in seven, one adversarial class is drawn and applied last, on top of the benign ones.
Every HTML response carries `<meta name="sd-mutation-seed" content="<seed>;<names>">`, naming
the seed and the mutation classes actually applied that day -- the scorer reads this tag rather
than recomputing a schedule, so a stored snapshot stays checkable even after the catalogue grows.

## The date override

For verification only -- the product never sends it. Set `X-Testbench-Date` (an ISO date, on or
after the dataset's epoch) together with `X-Testbench-Signature`, the hex HMAC-SHA256 of the date
string under `TESTBENCH_OVERRIDE_SECRET`. An unsigned or mis-signed `X-Testbench-Date` header is
silently ignored -- the bench serves the real UTC day instead -- so a stranger probing the public
demo account learns nothing by sending the header without the secret.

## Reproducing a day's DOM

Adding a mutation class to the catalogue changes which mutations every day draws, past and
future, so a day's DOM is reproducible from `(date, commit)`, not from the date alone. Check out
the commit that served the day in question (not the current tip), then:

```bash
uv run python -c "import datetime as dt; from testbench.portal.hook import mutate_html; \
from testbench.portal.render import render_results; d = dt.date(2026, 9, 10); \
print(mutate_html(d, render_results(d)))"
```

and compare the `sd-mutation-seed` tag in the output against the stored snapshot's.

## The answer key

`testbench/answers/key.py` is ground truth for a bench day: the expected value and the expected
XPath for each field, computed from the dataset and a render marked with `data-sd-truth`. It is
importable by tests and the scorer only -- `selector_drift` never imports anything under
`testbench` (the `answer-key-isolation` import-linter contract), so the product under test cannot
see its own answer key.

## Running it

```bash
uv run uvicorn testbench.portal.app:create_app --factory --port 8080
```

or, as deployed:

```bash
docker compose up testbench caddy
```
