from __future__ import annotations

import datetime as dt
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from testbench.mutations.base import CatalogueEntry
from testbench.portal.day import utc_now
from testbench.portal.hook import MutationMiddleware
from testbench.portal.middleware import ACCESS_LOG, AccessLogMiddleware, DayMiddleware
from testbench.portal.render import render_error
from testbench.portal.routes import LoginRequired, router
from testbench.portal.settings import Settings

STATIC = Path(__file__).parent / "static"


async def _redirect_to_login(request: Request, exc: Exception) -> Response:
    return RedirectResponse("/login", status_code=302)


async def _html_error(request: Request, exc: Exception) -> Response:
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    return HTMLResponse(render_error(status), status_code=status)


def _configure_access_log() -> None:
    ACCESS_LOG.setLevel(logging.INFO)
    if not ACCESS_LOG.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s access %(message)s"))
        ACCESS_LOG.addHandler(handler)


def create_app(
    *, clock: Callable[[], dt.datetime] = utc_now, catalogue: Sequence[CatalogueEntry] | None = None
) -> FastAPI:
    """`catalogue=None` is production: the full CATALOGUE. Tests of the portal's own behaviour
    pass `catalogue=()`; `uvicorn --factory` passes nothing."""
    settings = Settings.from_env()
    app = FastAPI(
        title="Selector Drift test bench", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.add_exception_handler(LoginRequired, _redirect_to_login)
    app.add_exception_handler(StarletteHTTPException, _html_error)
    # add_middleware prepends, so the LAST added is the OUTERMOST. Outer -> inner:
    #   AccessLog -> Day -> Mutation -> Session -> routes
    # Day runs before Mutation so both the route and the hook read the same day from the scope.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="sd_bench_session",
        max_age=8 * 3600,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.add_middleware(MutationMiddleware, catalogue=catalogue)
    app.add_middleware(DayMiddleware, clock=clock, override_secret=settings.override_secret)
    app.add_middleware(AccessLogMiddleware)
    _configure_access_log()
    return app
