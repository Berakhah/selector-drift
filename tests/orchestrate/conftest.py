"""Fixtures for the orchestrate tests. Task 26A APPENDS its bench fixtures to this file.

Nothing here is autouse except `_reset_alerted_in_flow`, below: the unit tests in this directory
(run keys, deployments) must not touch Postgres or start a Prefect server (M26), and resetting a
plain `ContextVar` does neither.

`scope_record()` builds a fully concrete, synthetic scope record rather than reading the real
`scopes/*.yaml` files -- those still carry unresolved `<UPPER-CASE>` placeholders (day-one values,
P-2/P-3/P-4) that would fail `ScopeRecord` validation (e.g. `user_agent_contact` containing `<>`).
The real spec files under `specs/` carry no such placeholders, so they are read directly.
"""

from __future__ import annotations

import datetime as dt
import functools
import socket
import threading
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from prefect.testing.utilities import prefect_test_harness
from sqlalchemy import text

from selector_drift.config import get_settings
from selector_drift.db.session import engine
from selector_drift.orchestrate.flows import _ALERTED_IN_FLOW

REPO = Path(__file__).resolve().parents[2]
PAGE = (
    "<!doctype html><html><head><title>Day one</title></head><body><h1>Day one</h1></body></html>"
)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def scope_record(target: str, origin: str, **overrides: object) -> dict[str, object]:
    """A synthetic, fully-resolved scope record: the gate is exercised, never bypassed, and no
    day-one placeholder ever reaches it."""
    rec: dict[str, object] = {
        "target": target,
        "basis": "self-owned",
        "authorising_party": "Selector Drift test harness",
        "authorised_on": (_today() - dt.timedelta(days=1)).isoformat(),
        "expires_on": (_today() + dt.timedelta(days=365)).isoformat(),
        "evidence_uri": None,
        "evidence_sha256": None,
        "origins": [origin],
        "rate_limit": {"requests": 1, "per_seconds": 1},
        "user_agent_contact": "https://example.invalid/contact",
        "first_heal_policy": "auto_promote",
        "warmup_runs": 5,
        "legal_review": "self_drafted",
    }
    rec.update(
        target=target,
        origins=[origin],
        rate_limit={"requests": 1, "per_seconds": 1},
        authorised_on=_today() - dt.timedelta(days=1),
        expires_on=_today() + dt.timedelta(days=365),
    )
    rec.update(overrides)
    return rec


def write_scope(directory: Path, rec: dict[str, object]) -> None:
    (directory / f"{rec['target']}.yaml").write_text(
        yaml.safe_dump(rec, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _reset_alerted_in_flow() -> None:
    """Hookfix brief, round 1, Important finding: `_ALERTED_IN_FLOW` is a `ContextVar` shared
    across sequential in-process flow runs (Prefect 3.8.4 runs a sync flow body in the caller's
    context, with no `copy_context()`), and it turns out that same sharing reaches across test
    functions too -- pytest runs a module's tests in one process/thread, so a value one test
    leaves set would otherwise leak into the next. Reset before every test, not just the
    integration ones that call the flow, so a stray leak can never make one test's result depend
    on what ran before it."""
    _ALERTED_IN_FLOW.set(None)


@pytest.fixture(scope="session")
def prefect_api() -> Iterator[None]:
    with prefect_test_harness():
        yield


@pytest.fixture
def db() -> Iterator[None]:
    with engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE runs, run_attempts, heartbeats, resolutions, audit_log, "
                "scope_records RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def scope_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    d = tmp_path / "scopes"
    d.mkdir()
    monkeypatch.setenv("SD_SCOPE_DIR", str(d))
    get_settings.cache_clear()
    yield d
    get_settings.cache_clear()


@pytest.fixture
def local_site(tmp_path: Path, scope_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    # M-4 (final review): tests calling ONCE("specs/testbench.yaml") resolve that path under
    # SD_SPEC_DIR, which defaults to the relative Path("specs") -- cwd-dependent. Point it at the
    # repo's real specs/ absolutely, so these tests pass regardless of where pytest is invoked
    # from.
    monkeypatch.setenv("SD_SPEC_DIR", str(REPO / "specs"))
    get_settings.cache_clear()
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(PAGE, encoding="utf-8")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(root))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    write_scope(scope_dir, scope_record("testbench", url))
    monkeypatch.setattr("selector_drift.browser.registry.url_for", lambda target: url)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def dead_site(scope_dir: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    # M-4 (final review): same cwd-independence fix as local_site, above.
    monkeypatch.setenv("SD_SPEC_DIR", str(REPO / "specs"))
    get_settings.cache_clear()
    with socket.socket() as s:  # a port that was free a moment ago, now closed
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    write_scope(scope_dir, scope_record("testbench", url))
    monkeypatch.setattr("selector_drift.browser.registry.url_for", lambda target: url)
    return url


@pytest.fixture
def expired_spec(tmp_path: Path, scope_dir: Path) -> str:
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    body = (REPO / "specs" / "testbench.yaml").read_text(encoding="utf-8")
    (specs / "expired.yaml").write_text(
        body.replace("scopes/testbench.yaml", "scopes/expired.yaml"), encoding="utf-8"
    )
    write_scope(
        scope_dir,
        scope_record(
            "expired",
            "https://expired.example.invalid",
            authorised_on=_today() - dt.timedelta(days=400),
            expires_on=_today() - dt.timedelta(days=1),
        ),
    )
    return str(specs / "expired.yaml")


@pytest.fixture
def mismatch_spec(tmp_path: Path, scope_dir: Path) -> str:
    """I-1 (final review): a spec named for one target (`mismatch`) whose `scope_ref` names a
    different, validly-authorised target's scope record (`testbench`). This is the only thing
    that stops a spec for target A running under target B's authorisation -- CLAUDE.md rule 1,
    the spine of the whole system. Same pattern as `expired_spec`, above."""
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    body = (REPO / "specs" / "testbench.yaml").read_text(encoding="utf-8")
    # scope_ref stays "scopes/testbench.yaml" -- unchanged from the real spec -- while the file
    # itself is named "mismatch.yaml", so target_of(spec_path) == "mismatch" but the scope record
    # it loads names "testbench".
    (specs / "mismatch.yaml").write_text(body, encoding="utf-8")
    write_scope(scope_dir, scope_record("testbench", "https://testbench.example.invalid"))
    return str(specs / "mismatch.yaml")


# ------------------------------------------------------------------------------------------------
# Task 26A: the bench fixtures the pipeline tests use. APPENDED -- Task 6's fixtures above stay.
#
# They serve the real portal in-process on a free port with a fixed, settable clock, never the
# signed override (the product never sends one, bench spec 3). The scope record is Task 6's
# synthetic `scope_record()` with the bench's local origin, so the scope gate is exercised, not
# bypassed; only its rate limit is raised so a run does not sleep through the production limit.
# Every root the product writes to is a per-test tmp directory, and the redaction key is minted
# per test (never a literal in the repo).

import os  # noqa: E402
import secrets  # noqa: E402
from collections.abc import Callable, Sequence  # noqa: E402
from dataclasses import dataclass  # noqa: E402

from pydantic import SecretStr  # noqa: E402
from sqlalchemy.orm import Session as OrmSession  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import PlainTextResponse, Response  # noqa: E402

from selector_drift.credentials.provider import Credentials, NotSupported  # noqa: E402
from testbench.mutations.base import CatalogueEntry  # noqa: E402
from testbench.mutations.catalogue import CATALOGUE  # noqa: E402
from testbench.portal.app import create_app  # noqa: E402
from tests.testbench.conftest import _bench_env, _serve  # noqa: E402
from tests.testbench.helpers import first_day_scheduling  # noqa: E402

START = dt.date(2026, 1, 1)
FAST = {"requests": 100, "per_seconds": 1}  # the limiter still runs; it just waits less
_CLEARED_TABLES = "selector_cache, interventions"  # no FK to runs, so `db` does not reach them


@dataclass
class Bench:
    """The served portal. `day` is the portal's clock and may be moved between runs, so one
    bench can serve a clean day and then the day a mutation is scheduled."""

    url: str
    day: dt.date
    mutated_day: dt.date | None = None


class _DemoAccount:
    """The bench's public demo account (bench spec 3.2: printed in its README, not a secret).
    The spec's login steps need a username and password; SessionInjection has neither."""

    def get(self, target: object) -> Credentials:
        return Credentials(cookies=None, username="demo", password=SecretStr("demo"))

    def get_totp_code(self, target: object) -> str:
        raise NotSupported("the bench has no second factor")


def _point_product_at(
    url: str, scopes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, warmup_runs: int
) -> None:
    # Authorised from START, so a test may run the product for any window in the bench's year,
    # not only today (the flow's window) and yesterday.
    rec = scope_record(
        "testbench", url, rate_limit=FAST, authorised_on=START, warmup_runs=warmup_runs
    )
    write_scope(scopes, rec)
    for name, sub in (
        ("SD_LAKE_ROOT", "lake"),
        ("SD_EVIDENCE_ROOT", "evidence"),
        ("SD_SESSION_DIR", "sessions"),
        ("SD_ESCALATION_CORPUS", "corpus"),
    ):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("SD_SPEC_DIR", str(REPO / "specs"))  # absolute: cwd-independent (M-4)
    monkeypatch.setenv("SD_REDACTION_KEY", os.urandom(32).hex())  # per test, never in the repo
    get_settings.cache_clear()  # after SD_SCOPE_DIR and friends are set
    monkeypatch.setattr("selector_drift.browser.registry.url_for", lambda target: url)
    monkeypatch.setattr(
        "selector_drift.credentials.registry.provider_for", lambda target: _DemoAccount()
    )
    with engine.begin() as c:  # the per-worker *_test database only (tests/conftest.py)
        c.execute(text(f"TRUNCATE {_CLEARED_TABLES} RESTART IDENTITY CASCADE"))


def _clock(state: Bench) -> Callable[[], dt.datetime]:
    return lambda: dt.datetime.combine(state.day, dt.time(4, 12), tzinfo=dt.UTC)


def _bench(
    catalogue: Sequence[CatalogueEntry],
    day: dt.date,
    scopes: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutated_day: dt.date | None = None,
    middleware: Callable[[Any], None] | None = None,
    warmup_runs: int = 5,
) -> Iterator[Bench]:
    _bench_env(monkeypatch, secrets.token_hex(32))
    state = Bench(url="", day=day, mutated_day=mutated_day)
    app = create_app(clock=_clock(state), catalogue=tuple(catalogue))
    if middleware is not None:
        middleware(app)
    for url in _serve(app):
        state.url = url
        _point_product_at(url, scopes, tmp_path, monkeypatch, warmup_runs=warmup_runs)
        yield state
    get_settings.cache_clear()


def _only(name: str) -> tuple[CatalogueEntry, ...]:
    return tuple(e for e in CATALOGUE if e.name == name)


def _mutated(name: str) -> tuple[dt.date, dt.date]:
    """(a clean day, the next day -- the first on which `name` is scheduled)."""
    first = first_day_scheduling(name, START + dt.timedelta(days=1))
    return first - dt.timedelta(days=1), first


@pytest.fixture
def session(db: None) -> Iterator[OrmSession]:
    from selector_drift.db.session import Session

    with Session() as s:
        yield s


@pytest.fixture
def bench(
    db: None, scope_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Bench]:
    yield from _bench((), START, scope_dir, tmp_path, monkeypatch)  # no mutations at all


@pytest.fixture
def bench_mutated(
    db: None, scope_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Bench]:
    """LabelReword only, on a day it is scheduled; `day` starts on the clean day before. A
    warm-up of one accepted run, so a heal of a distributional field can be judged after a
    single promotion (with the bench's five, the gate holds it as warm-up -- by design)."""
    clean, first = _mutated("LabelReword")
    yield from _bench(
        _only("LabelReword"), clean, scope_dir, tmp_path, monkeypatch, mutated_day=first,
        warmup_runs=1,
    )  # fmt: skip


@pytest.fixture
def bench_decoy(
    db: None, scope_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Bench]:
    """DecoyColumn only (the plausible-but-wrong adjacent column); `day` starts clean. Warm-up
    of one, as bench_mutated, so a refusal is the gate's judgement and not the warm-up."""
    clean, first = _mutated("DecoyColumn")
    yield from _bench(
        _only("DecoyColumn"), clean, scope_dir, tmp_path, monkeypatch, mutated_day=first,
        warmup_runs=1,
    )  # fmt: skip


@pytest.fixture
def flaky_bench(
    db: None, scope_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Bench]:
    """503 for the first request, then normal service: the first attempt fails (the driver
    refuses a 5xx page), a retry succeeds, and the failed attempt survives in run_attempts."""
    calls = {"n": 0}

    def install(app: Any) -> None:
        @app.middleware("http")  # type: ignore[misc]
        async def flake(request: Request, call_next: Callable[..., Any]) -> Response:
            calls["n"] += 1
            if calls["n"] <= 1:
                return PlainTextResponse("unavailable", status_code=503)
            response: Response = await call_next(request)
            return response

    yield from _bench((), START, scope_dir, tmp_path, monkeypatch, middleware=install)


@pytest.fixture
def demo_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """For Task 6's local_site / dead_site tests, which now run the whole pipeline: the target's
    real provider (SessionInjection) has no session file there and would fail every run before
    the page it is testing is ever reached."""
    monkeypatch.setattr(
        "selector_drift.credentials.registry.provider_for", lambda target: _DemoAccount()
    )
