import subprocess

import pytest


@pytest.mark.unit
def test_import_contracts_pass():
    r = subprocess.run(["uv", "run", "lint-imports"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.unit
def test_answer_key_contract_is_declared():
    assert "answer-key-isolation" in open(".importlinter").read()
