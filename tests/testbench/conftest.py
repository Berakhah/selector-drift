from __future__ import annotations

import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from testbench.portal.app import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve(app: FastAPI) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("bench server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def _bench_env(mp: pytest.MonkeyPatch, override_secret: str) -> None:
    # Generated per session: no secret, test or otherwise, is ever a literal in the repo.
    mp.setenv("TESTBENCH_SESSION_SECRET", secrets.token_hex(32))
    mp.setenv("TESTBENCH_OVERRIDE_SECRET", override_secret)
    mp.setenv("TESTBENCH_COOKIE_SECURE", "0")  # the test server is plain http


@pytest.fixture(scope="session")
def override_secret() -> str:
    return secrets.token_hex(32)


@pytest.fixture(scope="session")
def bench_server(override_secret: str) -> Iterator[str]:
    with pytest.MonkeyPatch.context() as mp:
        _bench_env(mp, override_secret)
        app = create_app()
    yield from _serve(app)


@pytest.fixture
def client_factory(
    monkeypatch: pytest.MonkeyPatch, override_secret: str
) -> Callable[..., TestClient]:
    _bench_env(monkeypatch, override_secret)

    def make(**kwargs: Any) -> TestClient:
        return TestClient(create_app(**kwargs), follow_redirects=False)

    return make
