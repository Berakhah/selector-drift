"""The bench-scorer compose service: it holds the redaction key and reads client heal bundles,
so its blast radius is pinned here rather than trusted to a comment."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SERVICES: dict[str, Any] = yaml.safe_load((REPO / "docker-compose.yml").read_text("utf-8"))[
    "services"
]
SCORER = SERVICES.get("bench-scorer", {})
FAIL_CLOSED = r"\$\{{{name}:\?\S.*\}}"


@pytest.mark.unit
def test_the_service_exists_and_runs_the_bench_side_entrypoint() -> None:
    assert SCORER, "docker-compose.yml has no bench-scorer service"
    assert SCORER["command"] == ["python", "-m", "testbench.serve_scorer"]
    assert SCORER["build"]["dockerfile"] == "docker/Dockerfile.bench-scorer"
    assert SCORER["restart"] == "unless-stopped"


@pytest.mark.unit
def test_it_is_hardened_like_every_other_service() -> None:
    assert SCORER["read_only"] is True
    assert SCORER["user"] == "10001:10001"
    assert SCORER["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in SCORER["security_opt"]
    assert not any("unconfined" in opt for opt in SCORER["security_opt"])
    assert "/tmp" in SCORER["tmpfs"]
    assert SCORER["mem_limit"] and SCORER["pids_limit"]
    assert "ports" not in SCORER and "cap_add" not in SCORER
    assert SCORER["init"] is True


@pytest.mark.unit
def test_it_connects_as_the_narrow_role_never_the_product_user() -> None:
    url = SCORER["environment"]["SD_DATABASE_URL"]
    assert url.startswith("postgresql+psycopg://sd_scorer:")
    assert "POSTGRES_USER" not in url and "POSTGRES_PASSWORD" not in url
    assert re.search(FAIL_CLOSED.format(name="SD_SCORER_PASSWORD"), url)


@pytest.mark.unit
def test_it_mounts_only_the_evidence_subpath_read_only() -> None:
    (mount,) = SCORER["volumes"]
    assert isinstance(mount, dict), "use the long form: the short form cannot carry a subpath"
    assert mount["source"] == "sd_data"
    assert mount["target"] == "/data/evidence"
    assert mount["read_only"] is True
    assert mount["volume"]["subpath"] == "evidence"
    assert SCORER["environment"]["SD_EVIDENCE_ROOT"] == "/data/evidence"


@pytest.mark.unit
def test_it_holds_the_same_redaction_key_as_the_worker_and_fails_closed() -> None:
    worker = SERVICES["worker"]["environment"]["SD_REDACTION_KEY"]
    scorer = SCORER["environment"]["SD_REDACTION_KEY"]
    assert scorer == worker
    assert re.fullmatch(FAIL_CLOSED.format(name="SD_REDACTION_KEY"), scorer)


@pytest.mark.unit
def test_it_has_no_browser_and_no_session_or_lake_paths() -> None:
    env = SCORER["environment"]
    assert not {"PLAYWRIGHT_BROWSERS_PATH", "SD_SESSION_DIR", "SD_LAKE_ROOT"} & set(env)
    assert "shm_size" not in SCORER


@pytest.mark.unit
def test_env_example_documents_a_password_step_that_works_and_holds_no_secret() -> None:
    env = (REPO / ".env.example").read_text("utf-8")
    assert "set -a" in env  # the snippet loads .env, so the variables are not empty
    assert "log_min_error_statement" in env  # a failed ALTER ROLE must not log the password
    assert re.search(r"^SD_SCORER_PASSWORD=\s*(#.*)?$", env, re.MULTILINE)


@pytest.mark.unit
def test_the_dockerfile_installs_no_browser_and_runs_non_root() -> None:
    text = (REPO / "docker" / "Dockerfile.bench-scorer").read_text("utf-8")
    assert "playwright install" not in text
    assert "COPY testbench" in text and "COPY selector_drift" in text
    assert "USER 10001:10001" in text
    assert 'CMD ["python", "-m", "testbench.serve_scorer"]' in text
