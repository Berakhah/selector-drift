from __future__ import annotations

import re
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
def test_the_caddy_container_is_hardened() -> None:
    """S-I1. Caddy is the one container with ports bound to the host on a public VM, so it gets
    the same hardening the bench service has, with one documented exception: the official
    caddy:2.8 image has no built-in non-root user and (verified against the real image,
    docker-compose.yml's own comment records the exact failure) cannot write
    /data/caddy or /config/caddy as a non-root UID on a fresh volume, so `user:` is left at the
    image default (root) -- cap_drop: ALL still applies to root, so cap_add: [NET_BIND_SERVICE]
    is what lets it bind 80/443 at all."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    caddy = compose["services"]["caddy"]
    assert caddy["read_only"] is True
    assert caddy["cap_drop"] == ["ALL"]
    assert caddy["cap_add"] == ["NET_BIND_SERVICE"]
    assert "no-new-privileges:true" in caddy["security_opt"]
    assert not any("unconfined" in opt for opt in caddy["security_opt"])
    assert "/tmp" in caddy["tmpfs"]
    assert caddy["mem_limit"]
    assert caddy["pids_limit"]


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
def test_ci_declares_least_privilege_permissions_and_pins_actions_to_shas() -> None:
    """S-I3/S-I4: the job writes nothing back to GitHub (push: false), so GITHUB_TOKEN needs
    read-only; every `uses:` is pinned to a commit SHA (a mutable tag is repointable by the
    publisher or anyone who compromises it), with a trailing comment naming the tag."""
    text = (REPO / ".github/workflows/testbench-image.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["build-arm64"]["steps"]
    uses = [step["uses"] for step in steps if "uses" in step]
    assert uses, "expected at least one uses: step"
    for u in uses:
        action, _, ref = u.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{action} is not pinned to a commit SHA: {u!r}"
    assert all("# v" in line for line in text.splitlines() if "uses:" in line)


@pytest.mark.unit
def test_the_dockerignore_excludes_the_answer_key() -> None:
    """S-I6: the answer key is ground truth for the scorer and tests only -- the import-linter
    contract keeps selector_drift from importing it, but says nothing about what ends up on
    disk inside the publicly-deployed container, so it must be re-excluded after the
    `!testbench/**` re-include."""
    lines = (
        (REPO / "testbench" / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    )
    reinclude = next(i for i, line in enumerate(lines) if line.strip() == "!testbench/**")
    exclude = next(
        (i for i, line in enumerate(lines) if line.strip() == "testbench/answers/"), None
    )
    assert exclude is not None, "testbench/answers/ is not excluded from the shipped image"
    assert exclude > reinclude, "the exclusion must come after the !testbench/** re-include"


@pytest.mark.unit
def test_the_caddyfile_sets_security_headers() -> None:
    """S-I5. No CSP: VirtualisedRows is the one bench mutation that injects an inline <script>
    (testbench/mutations/adversarial/virtualised_rows.py), and a CSP strict enough to matter
    would need to special-case it."""
    caddyfile = (REPO / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    for header in (
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
    ):
        assert header in caddyfile, header


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
