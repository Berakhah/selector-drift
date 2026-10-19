import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel

from selector_drift.api import views
from selector_drift.api.deps import check_run_key, check_target
from selector_drift.api.errors import Invalid
from selector_drift.api.main import create_app
from selector_drift.config import ConfigError, Settings

REPO = Path(__file__).resolve().parents[2]

REQUEST_MODELS = [
    m
    for _, m in inspect.getmembers(views, inspect.isclass)
    if issubclass(m, views.RequestModel) and m is not views.RequestModel
]


@pytest.mark.unit
def test_no_request_model_declares_actor():
    """H3. The actor is the session operator. A body carrying `actor` is a 422."""
    assert REQUEST_MODELS, "views.py declares no request models"
    for model in REQUEST_MODELS:
        assert "actor" not in model.model_fields, model.__name__


@pytest.mark.unit
def test_every_request_model_forbids_extra_fields():
    for model in REQUEST_MODELS:
        assert model.model_config.get("extra") == "forbid", model.__name__


@pytest.mark.unit
def test_every_route_body_is_a_request_model():
    """A body model outside views.RequestModel would escape both tests above."""
    for route in create_app(settings=Settings.for_typegen()).routes:
        if isinstance(route, APIRoute) and route.body_field is not None:
            body = route.body_field.type_
            assert isinstance(body, type) and issubclass(body, views.RequestModel), route.path


@pytest.mark.unit
def test_no_view_has_a_boolean_that_means_passed():
    """API spec 1, rule 3: a check result crosses the wire as a Verdict, never a bool."""
    suspicious = {"passed", "ok", "valid", "is_valid", "success", "succeeded"}
    for _, model in inspect.getmembers(views, inspect.isclass):
        if issubclass(model, BaseModel):
            assert not suspicious & set(model.model_fields), model.__name__


@pytest.mark.unit
def test_stats_view_declares_false_accepts():
    """Spec 10.1: a wrong heal the gate let through is published; None without an answer key."""
    field = views.StatsView.model_fields["false_accepts"]
    assert type(None) in field.annotation.__args__  # type: ignore[union-attr]


@pytest.mark.unit
@pytest.mark.parametrize("good", ["testbench:1:2026-09-07", "a-b:123456:2027-01-31"])
def test_a_well_formed_run_key_passes(good):
    assert check_run_key(good) == good


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "testbench:1:2026-9-7",
        "TB:1:2026-09-07",
        "testbench:1:2026-09-07/..",
        "testbench:1234567:2026-09-07",
        "",
    ],
)
def test_a_malformed_run_key_is_422_before_any_lookup(bad):
    with pytest.raises(Invalid):
        check_run_key(bad)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["-lead", "UPPER", "a" * 64, "a/b", "a..b", ""])
def test_a_malformed_target_is_422(bad):
    with pytest.raises(Invalid):
        check_target(bad)


@pytest.mark.unit
def test_api_imports_nothing_that_can_start_a_run():
    # Same pattern as tests/test_import_contracts.py: call the entry-point function with this
    # interpreter (no `uv` on PATH needed), and require the contract to be NAMED in the output
    # so an unknown or absent contract cannot pass vacuously.
    code = (
        "from importlinter.cli import lint_imports_command; "
        "lint_imports_command(['--contract', 'api-is-read-side'])"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        encoding="utf-8",
        cwd=REPO,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    # import-linter prints a contract's `name`, not its id, so name the contract by its title.
    assert "The API reports on runs; it never starts one KEPT" in r.stdout, r.stdout
    assert "1 kept, 0 broken" in r.stdout, r.stdout


@pytest.mark.unit
def test_fixture_mode_refuses_to_start_in_production():
    import dataclasses

    s = dataclasses.replace(Settings.for_typegen(), env="production", fixture_mode=True)
    with pytest.raises(ConfigError, match="SD_FIXTURE_MODE"):
        create_app(settings=s)


@pytest.mark.unit
def test_the_openapi_document_is_not_served_over_http():
    app = create_app(settings=Settings.for_typegen())
    assert app.openapi_url is None and app.docs_url is None and app.redoc_url is None
