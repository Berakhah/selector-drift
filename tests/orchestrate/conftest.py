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
