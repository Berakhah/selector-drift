from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from testbench.portal.day import request_day

ACCESS_LOG = logging.getLogger("testbench.access")
BENCH_DAY = "bench_day"


class DayMiddleware:
    """Computes the request's day once and stores it in the scope state. The route renders that
    day's dataset and the mutation hook (Task 8) mutates with that day's schedule."""

    def __init__(
        self, app: ASGIApp, *, clock: Callable[[], dt.datetime], override_secret: bytes | None
    ) -> None:
        self.app = app
        self._clock = clock
        self._secret = override_secret

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            day = request_day(Headers(scope=scope), now=self._clock(), override_secret=self._secret)
            scope.setdefault("state", {})[BENCH_DAY] = day
        await self.app(scope, receive, send)


def day_of(scope: Scope) -> dt.date:
    day = scope.get("state", {}).get(BENCH_DAY)
    if not isinstance(day, dt.date):
        raise RuntimeError("DayMiddleware did not run for this request")
    return day


class AccessLogMiddleware:
    """One line per request: method, path, status, User-Agent. Never the query string, never a
    cookie. It is how the bench checks that the runner identifies itself (brief §3.3)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500

        async def send_and_note(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_and_note)
        finally:
            agent = Headers(scope=scope).get("user-agent", "-")
            ACCESS_LOG.info(
                "%s %s %d ua=%r",
                scope["method"],
                scope["path"],
                status,
                agent,
                extra={"http_path": scope["path"], "http_status": status, "user_agent": agent},
            )
