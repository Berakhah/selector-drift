"""RFC 9457 problem+json (API spec 5). Every error the API returns has this shape."""

from __future__ import annotations

from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from selector_drift.validate.verdict import Verdict

PROBLEM_JSON = "application/problem+json"


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    failing: list[Verdict] | None = None  # still-invalid only


class ApiProblem(Exception):
    slug: ClassVar[str] = "invalid"
    status: ClassVar[int] = 400
    title: ClassVar[str] = "Bad request"

    def __init__(
        self,
        detail: str,
        *,
        failing: list[Verdict] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.failing = failing
        self.headers = headers or {}


class Unauthenticated(ApiProblem):
    slug, status, title = "unauthenticated", 401, "Not signed in"


class BadOrigin(ApiProblem):
    slug, status, title = "bad-origin", 403, "Request origin refused"


class NotFound(ApiProblem):
    slug, status, title = "not-found", 404, "No such resource"


class Invalid(ApiProblem):
    slug, status, title = "invalid", 422, "The request is not valid"


class StillInvalidProblem(ApiProblem):
    slug, status, title = "still-invalid", 409, "Promotion refused: the gate still fails"


class AlreadyResolved(ApiProblem):
    slug, status, title = "already-resolved", 409, "This run has already been decided"


class Throttled(ApiProblem):
    slug, status, title = "throttled", 429, "Too many sign-in attempts"


class EvidenceGone(ApiProblem):
    """410. Corrupted (a manifest mismatch) or expired (deleted under retention). The file
    is never served in either case (API spec 7.3)."""

    status, title = 410, "Evidence is not available"

    def __init__(self, detail: str, *, reason: str) -> None:
        super().__init__(detail)
        self.reason = reason

    @property
    def problem_slug(self) -> str:
        return f"evidence-{self.reason}"


def _slug(problem: ApiProblem) -> str:
    return problem.problem_slug if isinstance(problem, EvidenceGone) else problem.slug


def problem_response(problem: ApiProblem, base_url: str) -> JSONResponse:
    body = ProblemDetail(
        type=f"{base_url.rstrip('/')}/problems/{_slug(problem)}",
        title=problem.title,
        status=problem.status,
        detail=problem.detail,
        failing=problem.failing,
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=problem.status,
        media_type=PROBLEM_JSON,
        headers=problem.headers,
    )


def _base(request: Request) -> str:
    return str(request.app.state.settings.public_base_url)


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiProblem)
    async def _problem(request: Request, exc: ApiProblem) -> JSONResponse:
        return problem_response(exc, _base(request))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Location and message only. FastAPI's default echoes `input` -- which, on
        # /auth/login, is the password.
        parts: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            parts.append(f"{loc}: {err.get('msg', 'invalid')}")
        return problem_response(Invalid("; ".join(parts) or "invalid request"), _base(request))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        problem: ApiProblem = (
            NotFound("no route or resource at this path")
            if exc.status_code == 404
            else Invalid(str(exc.detail))
        )
        response = problem_response(problem, _base(request))
        if exc.status_code not in (404, 422):
            response.status_code = exc.status_code
        return response
