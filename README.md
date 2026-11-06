# Selector Drift

Self-healing data extraction for business portals with no API.

When a portal's UI changes, Selector Drift re-finds each field and then has to prove the new match is right before any data is written. If it can't, the run is held for a person to review. A selector that breaks throws an error, but one that heals onto the wrong column returns plausible, wrong numbers for weeks, so the validation gate is the centre of the design.

**Status: in development.** The pipeline, validation gate, evidence store and test bench exist. The web UI and HTTP API don't, and a fresh clone won't install yet ([details](#status-and-known-limitations)).

## How a run works

```mermaid
flowchart TD
    A[Task spec and target] --> B{Scope record valid?}
    B -- "unknown or expired" --> X[Refuse the run]
    B -- yes --> C[Rung 1: Cached selector]
    C -- "still valid" --> G
    C -- broken --> D[Rung 2: Accessibility tree]
    D -- miss --> E[Rung 3: Text anchors]
    E -- miss --> F[Rung 4: Structural heuristics]
    F -- miss --> R[Rung 5: Escalate to a person]
    D -- found --> G{Validation gate}
    E -- found --> G
    F -- found --> G
    G -- pass --> L[Write to Parquet lake and heal log]
    G -- "fail or not enough evidence" --> Q[Quarantine with evidence]
    R --> Q
    Q -- "person promotes" --> L
    Q -- "person discards" --> Z[Discard]
```

Rung 5 is reserved for visual grounding with a model, but v1 makes no model calls, so it hands the field to a person.

The gate's checks live in [`selector_drift/validate/assertions/`](selector_drift/validate/assertions/):

| Check | What it catches |
| --- | --- |
| `FormatAssertion` | Values of the wrong type or format |
| `DomainAssertion` | Values outside an allowed range or set |
| `CrossFieldAssertion` | Fields that no longer agree with each other |
| `DistributionalAssertion` | A shift from recent runs, such as locking onto the adjacent column, where each value looks valid on its own |
| `RowKeyContinuity` | Values that no longer match the last accepted run, joined on the natural key |
| `IndependentRungAgreement` | A heal where a rung from a different signal family lands on a different node |
| `FirstHealConfirmation` | A selector seen for the first time, which needs a person to confirm it |

## Measured results

These come from the project's own [test bench](testbench/README.md), a deliberately mutating claims portal. They are not production results. [`docs/methodology.md`](docs/methodology.md) has the method, the known limits and how to reproduce them.

| Measure (2026-10-16) | Result | What it means |
| --- | --- | --- |
| Benign acceptance | 8/8 mutation classes | On one pinned day, with history; the result varies by day |
| Adversarial refusal | 3/7 classes | The other 4 were written, from the correct nodes |
| Wrong-node writes | 0/7 classes | Checked against the answer key |
| Offline floor | 8/8 benign classes | Rungs 1-4 only, with all network access and model providers blocked |

An earlier 60-day campaign measured which rung resolved each field: rung 1 16.9%, rung 2 69.7%, rung 3 0%, rung 4 13.1%, rung 5 0%. It ran before a bench defect was found and hasn't been re-run on the fixed bench, so its 9 false refusals say nothing about the gate. See the erratum in the methodology.

## Safety rules

- **Scope:** no target runs without a current scope record stating who authorised it.
- **Access:** no bypassing authentication or anti-bot measures. A target that needs evasion is out of scope.
- **Credentials:** the default is a session the client exports after logging in. Stored passwords are a last resort, and TOTP seeds are never stored ([`docs/security.md`](docs/security.md)).
- **Redaction:** screenshots and DOM snapshots are redacted before storage.

## Status and known limitations

**Not built**

- There is no web UI or HTTP API. The API work is in open [PR #1](https://github.com/Berakhah/selector-drift/pull/1).
- The ninety-day live heal log hasn't been run, so there are no results from a live portal.

**Install**

- `bok-core` isn't on PyPI, and `pyproject.toml` points to an absolute path on the author's machine. Until that changes, `uv sync` won't work on a fresh clone.

**CI**

- Tests don't run in CI on `main`, which only builds the test bench image. The test workflow is in draft [PR #4](https://github.com/Berakhah/selector-drift/pull/4). Conflicts need resolving before it can merge.

**Hardening still in draft PRs**

- [#7](https://github.com/Berakhah/selector-drift/pull/7) blocks browser writes after login. Until it merges, "no write-back" holds only because the product has no write path.
- [#6](https://github.com/Berakhah/selector-drift/pull/6) gives the worker a least-privilege Postgres role. Today it connects as the Compose superuser.
- [#5](https://github.com/Berakhah/selector-drift/pull/5) holds a run that lost most of its rows.

**Gate limits**

- With no history, a value attack on the correct nodes can only be caught by the person confirming run one.
- A portal that flips between two layouts asks for confirmation on every flip, because the cache holds one selector per field.
- Rung 3 resolved nothing on the bench, because rung 2 looks for the same label text and runs first.

## Layout

```text
selector_drift/
  scope/        scope records and the authorisation gate
  spec/         task spec schema
  browser/      containerised Playwright driver
  credentials/  credential postures and envelope encryption
  resolve/      the cascade and selector cache
  validate/     the gate, its checks and quarantine
  extract/      typed extraction
  quality/      schema drift, null rates, row counts
  land/         Parquet writer and DuckDB catalog
  orchestrate/  the Prefect pipeline and rung statistics
  heallog/      evidence store, redaction, replay
testbench/      mutating portal, mutation engine, answer-key scorer
tests/          1,434 Python test functions (counted 2026-11-05)
```

Built with Python 3.12, Playwright, Pydantic, PostgreSQL, Alembic, Prefect, Parquet, DuckDB and Docker Compose.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
