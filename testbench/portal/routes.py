from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from testbench.portal import render
from testbench.portal.middleware import day_of

router = APIRouter()

# The public demo account (bench spec §3.2). Printed in the README; not a secret. It exists to
# exercise the product's credential path against a login form.
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo"


class LoginRequired(Exception):
    """Raised by require_login; app.py maps it to 302 /login."""


def require_login(request: Request) -> None:
    if request.session.get("user") != DEMO_USERNAME:
        raise LoginRequired


def _same(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _query(
    claim_number: str, claimant: str, status: str, filed_from: str, filed_to: str
) -> render.SearchQuery:
    return render.SearchQuery.parse(
        claim_number=claim_number,
        claimant=claimant,
        status=status,
        filed_from=filed_from,
        filed_to=filed_to,
    )


@router.get("/")
def home(request: Request) -> RedirectResponse:
    require_login(request)
    return RedirectResponse("/search", status_code=302)


@router.get("/login")
def login_form() -> HTMLResponse:
    return HTMLResponse(render.render_login())


@router.post("/login")
def login(
    request: Request, username: Annotated[str, Form()] = "", password: Annotated[str, Form()] = ""
) -> Response:
    if _same(username, DEMO_USERNAME) and _same(password, DEMO_PASSWORD):
        request.session.clear()
        request.session["user"] = DEMO_USERNAME
        return RedirectResponse("/search", status_code=303)
    return HTMLResponse(render.render_login(error="Unknown username or password."), status_code=401)


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/search")
def search(request: Request) -> HTMLResponse:
    require_login(request)
    return HTMLResponse(render.render_search())


@router.get("/results")
def results(
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    claim_number: str = "",
    claimant: str = "",
    status: str = "",
    filed_from: str = "",
    filed_to: str = "",
) -> HTMLResponse:
    require_login(request)
    query = _query(claim_number, claimant, status, filed_from, filed_to)
    return HTMLResponse(render.render_results(day_of(request.scope), page, query))


@router.get("/claims/{claim_number}")
def claim_detail(request: Request, claim_number: str) -> HTMLResponse:
    require_login(request)
    html = render.render_detail(day_of(request.scope), claim_number)
    if html is None:
        raise HTTPException(status_code=404)
    return HTMLResponse(html)


@router.get("/export.csv")
def export_csv(
    request: Request,
    claim_number: str = "",
    claimant: str = "",
    status: str = "",
    filed_from: str = "",
    filed_to: str = "",
) -> Response:
    require_login(request)
    query = _query(claim_number, claimant, status, filed_from, filed_to)
    return Response(
        render.results_csv(day_of(request.scope), query),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="claims.csv"'},
    )


@router.get("/healthz")
def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")
