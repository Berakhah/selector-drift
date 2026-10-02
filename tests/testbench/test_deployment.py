from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from testbench.mutations.catalogue import CATALOGUE

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_the_bench_container_is_hardened() -> None:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    bench = compose["services"]["testbench"]
    assert bench["read_only"] is True
    assert bench["user"] == "10001:10001"
    assert bench["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in bench["security_opt"]
    assert not any("unconfined" in opt for opt in bench["security_opt"])
    assert "/tmp" in bench["tmpfs"]
    assert "ports" not in bench, "only Caddy is published; the bench is reachable through it"


@pytest.mark.unit
def test_the_image_runs_the_factory_as_a_non_root_user() -> None:
    dockerfile = (REPO / "testbench" / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert '"testbench.portal.app:create_app", "--factory"' in dockerfile
    assert "--only-group testbench" in dockerfile


@pytest.mark.unit
def test_ci_proves_the_arm64_build() -> None:
    workflow = yaml.safe_load(
        (REPO / ".github/workflows/testbench-image.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["build-arm64"]["steps"]
    assert any(step.get("with", {}).get("platforms") == "linux/arm64" for step in steps)


@pytest.mark.unit
def test_env_example_declares_the_bench_secrets_without_values() -> None:
    pairs = dict(
        line.split("=", 1)
        for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert pairs["TESTBENCH_SESSION_SECRET"] == ""
    assert pairs["TESTBENCH_OVERRIDE_SECRET"] == ""
    assert "BENCH_HOST" in pairs


@pytest.mark.unit
def test_the_readme_documents_every_mutation_class() -> None:
    """DoD 5: every mutation class, the seeding, and how to reproduce a day's DOM."""
    readme = (REPO / "testbench" / "README.md").read_text(encoding="utf-8")
    assert len(CATALOGUE) == 15
    for entry in CATALOGUE:
        assert f"`{entry.name}`" in readme, entry.name
    for phrase in ("sd-mutation-seed", "X-Testbench-Signature", "mutate_html", "sha256"):
        assert phrase in readme
