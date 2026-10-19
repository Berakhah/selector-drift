"""The app factory: problem+json handlers, the Origin guard, the auth router."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response

from selector_drift.api import auth
from selector_drift.api.errors import BadOrigin, install_problem_handlers, problem_response
from selector_drift.config import ConfigError, Settings, get_settings

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def _production_web_origin(raw: str) -> str:
    """The exact Origin a browser will send for SD_WEB_ORIGIN, or ConfigError."""
    bad = ConfigError("SD_WEB_ORIGIN must be https://host[:port] with no path, query or fragment")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise bad from None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme.lower() != "https"
        or not host
        or host in _LOOPBACK
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise bad
    shown = f"[{host}]" if ":" in host else host
    return f"https://{shown}" if port in (None, 443) else f"https://{shown}:{port}"


def install_origin_guard(app: FastAPI) -> None:
    """CSRF: SameSite=Strict, plus the Origin of every non-GET must be the web origin."""

    @app.middleware("http")
    async def origin_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings: Settings = request.app.state.settings
        if (
            request.method not in SAFE_METHODS
            and request.headers.get("origin") != request.app.state.web_origin
        ):
            return problem_response(
                BadOrigin("this request did not come from the web origin"),
                settings.public_base_url,
            )
        return await call_next(request)


def create_app(*, settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    if s.fixture_mode and s.env == "production":
        raise ConfigError("SD_FIXTURE_MODE=1 refuses to start with SD_ENV=production")
    app = FastAPI(
        title="Selector Drift API",
        version="1.0.0",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,  # schema exported offline
    )
    app.state.settings = s
    app.state.web_origin = (
        _production_web_origin(s.web_origin) if s.env == "production" else s.web_origin
    )
    install_problem_handlers(app)
    install_origin_guard(app)
    app.include_router(auth.router, prefix="/api/v1")
    return app
