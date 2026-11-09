# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[SemVer](https://semver.org/); before 1.0 a minor bump can break things.

## [Unreleased]

## [0.1.0] - 2026-11-09

First tagged release. It marks the point where a fresh clone installs and CI runs the tests. It
is not production-ready: there is no web UI or HTTP API, and no live-portal heal log yet (see
"Status and known limitations" in the README).

### Added

- The extraction pipeline: scope gate, task spec, containerised Playwright driver, the
  resolution cascade (rungs 1-4; rung 5 hands the field to a person, since v1 makes no model
  calls), typed extraction, Parquet landing with a DuckDB catalog, and Prefect orchestration.
- The post-heal validation gate (`FormatAssertion`, `DomainAssertion`, `CrossFieldAssertion`,
  `DistributionalAssertion`, `RowKeyContinuity`, `IndependentRungAgreement`,
  `FirstHealConfirmation`) and quarantine with one-click promote or discard.
- Credential postures with envelope encryption, and redaction of screenshots and DOM snapshots
  before storage.
- The test bench: a deliberately mutating claims portal with benign and adversarial mutation
  classes and an answer key.
- Bench results as of 2026-10-16: benign acceptance 8/8 mutation classes, adversarial refusal
  3/7, wrong-node writes 0/7, offline floor 8/8. Method and limits in `docs/methodology.md`.
- `bok-core` 0.0.1 vendored as a pre-built wheel in `vendor/`, with its source commit, build
  command and sha256, replacing an absolute path to a sibling checkout.
- `.github/workflows/tests.yml`: ruff, `mypy --strict`, import contracts, the unit suite, and the
  integration suite against a throwaway Postgres and a real Chromium, on every push and PR.
- `.github/workflows/release.yml`: a version tag publishes a GitHub Release from this file, only
  if the tagged commit has a green test run.
- `SECURITY.md` with a private reporting route.

### Known gaps

- The 63 end-to-end tests don't run in CI.
- One unit test (unresolved scope-record placeholders) is deselected in CI until the
  ninety-day-log targets are chosen.
- Hardening still in draft PRs: GET-only browsing after login (#7), a least-privilege worker
  Postgres role (#6), holding a run that lost most of its rows (#5).

[Unreleased]: https://github.com/Berakhah/selector-drift/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Berakhah/selector-drift/releases/tag/v0.1.0
