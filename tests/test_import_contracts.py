import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# import-linter 2.14 exposes only console scripts (`lint-imports` ->
# `importlinter.cli:lint_imports_command`); `importlinter.cli` has no `__main__` block, so
# `python -m importlinter.cli` would import it, do nothing and exit 0 -- a vacuous pass. Call
# the entry-point function with this interpreter, so the test needs neither `uv` nor the
# venv's Scripts/ directory on PATH.
_LINT_IMPORTS = "from importlinter.cli import lint_imports_command; lint_imports_command()"


@pytest.mark.unit
def test_import_contracts_pass():
    # UTF-8 on both ends: lint-imports prints non-ASCII, and Windows' cp1252 default fails to
    # decode it in the reader thread, leaving stdout None.
    r = subprocess.run(
        [sys.executable, "-c", _LINT_IMPORTS],
        capture_output=True,
        encoding="utf-8",
        cwd=REPO,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Contracts: " in r.stdout and " broken" in r.stdout, r.stdout
    assert "0 broken" in r.stdout, r.stdout


@pytest.mark.unit
def test_answer_key_contract_is_declared():
    assert "answer-key-isolation" in (REPO / ".importlinter").read_text(encoding="utf-8")
