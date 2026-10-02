from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from testbench.portal.data import dataset_for
from testbench.portal.hook import mutate_html
from testbench.portal.render import render_error, render_login, render_results, results_csv
from testbench.schedule import scheduled_names, seed_for
from tests.testbench.helpers import bench_catalogue, login, root, sign_override

PORTAL = Path(__file__).resolve().parents[2] / "testbench" / "portal"
CATALOGUE = bench_catalogue()
NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.UTC)
DAY = NOW.date()
Factory = Callable[..., TestClient]


def seed_meta(html: str) -> str:
    metas = root(html).xpath('//head/meta[@name="sd-mutation-seed"]')
    assert len(metas) == 1, "exactly one seed tag, in <head>"
    return str(metas[0].get("content"))


@pytest.mark.unit
def test_every_html_response_carries_the_seed_meta_tag(client_factory: Factory) -> None:
    client = client_factory(clock=lambda: NOW, catalogue=CATALOGUE)
    names = ",".join(scheduled_names(DAY, "testbench", CATALOGUE))
    expected = f"{seed_for(DAY, 'testbench')};{names}"
    responses = [
        client.get("/login"),
        client.post("/login", data={"username": "demo", "password": "wrong"}),
    ]
    login(client)
    first = dataset_for(DAY)[0].claim_number
    responses += [
        client.get("/search"),
        client.get("/results"),
        client.get(f"/claims/{first}"),
        client.get("/claims/not-a-claim"),
        client.get("/static/missing.css"),
    ]
    for response in responses:
        assert response.headers["content-type"].startswith("text/html"), response.url
        assert seed_meta(response.text) == expected, response.url


@pytest.mark.unit
def test_csv_and_static_responses_are_never_mutated(client_factory: Factory) -> None:
    client = client_factory(clock=lambda: NOW, catalogue=CATALOGUE)
    login(client)
    exported = client.get("/export.csv")
    css = client.get("/static/portal.css")
    assert exported.text == results_csv(DAY)
    assert css.content == (PORTAL / "static" / "portal.css").read_bytes()
    assert client.get("/healthz").text == "ok"
    for response in (exported, css):
        assert "sd-mutation-seed" not in response.text and "data-test-" not in response.text


@pytest.mark.unit
def test_the_served_html_is_the_engines_output_for_the_request_day(client_factory: Factory) -> None:
    client = client_factory(clock=lambda: NOW, catalogue=CATALOGUE)
    assert client.get("/login").text == mutate_html(DAY, render_login(), catalogue=CATALOGUE)
    login(client)
    assert client.get("/results").text == mutate_html(DAY, render_results(DAY), catalogue=CATALOGUE)
    missing = client.get("/claims/not-a-claim")
    assert missing.status_code == 404
    assert missing.text == mutate_html(DAY, render_error(404), catalogue=CATALOGUE)


@pytest.mark.unit
def test_a_signed_override_steers_the_schedule_and_the_dataset_together(
    client_factory: Factory, override_secret: str
) -> None:
    client = client_factory(clock=lambda: NOW, catalogue=CATALOGUE)
    login(client)
    other = dt.date(2026, 9, 1)
    response = client.get("/results", headers=sign_override(other, override_secret))
    assert response.text == mutate_html(other, render_results(other), catalogue=CATALOGUE)


@pytest.mark.unit
def test_the_seed_tag_follows_the_utc_day(client_factory: Factory) -> None:
    """M20, at the hook: 23:30 in UTC-5 is served with the NEXT day's seed."""
    utc_minus_5 = dt.timezone(dt.timedelta(hours=-5))
    client = client_factory(
        clock=lambda: dt.datetime(2026, 9, 9, 23, 30, tzinfo=utc_minus_5), catalogue=CATALOGUE
    )
    seed = seed_for(dt.date(2026, 9, 10), "testbench")
    assert seed_meta(client.get("/login").text).startswith(seed)
