"""The label on stored evidence cannot be downgraded by the database alone.

`_stored_sensitivity` used to read `scope_records.basis` only. The worker writes that mirror
every run (it must), so anything that can write a row there -- a compromised worker, a stray
SQL statement -- could relabel a client's evidence SYNTHETIC and, with it, drop the retention
cutoff. The repo's scope YAML is a file the database cannot change, so SYNTHETIC now needs both
to say `self-owned`; anything else, an unreadable file included, is CLIENT_CONFIDENTIAL.

What this does NOT defend against is a worker process that is itself compromised (it can write
the files it can reach): see the 0003 migration docstring.

Callers: none (pytest collects tests/heallog/test_*.py). Instruction: "yes go" -- document the
worker role's limits and make the one reader that matters tamper-resistant.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bok_core.boundary.sensitivity import Sensitivity

from selector_drift.config import get_settings
from selector_drift.heallog import evidence as evidence_mod
from selector_drift.scope.record import Basis

SELF, CLIENT = Basis.SELF_OWNED.value, Basis.CLIENT_AUTHORISED.value


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mirror", "file", "expected"),
    [
        (SELF, SELF, Sensitivity.SYNTHETIC),
        (SELF, CLIENT, Sensitivity.CLIENT_CONFIDENTIAL),  # the relabel: mirror lies, file wins
        (CLIENT, SELF, Sensitivity.CLIENT_CONFIDENTIAL),
        (CLIENT, CLIENT, Sensitivity.CLIENT_CONFIDENTIAL),
        (SELF, None, Sensitivity.CLIENT_CONFIDENTIAL),  # no readable file: fail closed
        (None, SELF, Sensitivity.CLIENT_CONFIDENTIAL),  # no mirror row: fail closed
        (None, None, Sensitivity.CLIENT_CONFIDENTIAL),
    ],
)
def test_synthetic_needs_the_mirror_and_the_file_to_agree(
    mirror: str | None, file: str | None, expected: Sensitivity
) -> None:
    assert evidence_mod._sensitivity_for(mirror, file) is expected


@pytest.mark.unit
def test_a_missing_or_invalid_scope_file_reads_as_no_basis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setenv("SD_SCOPE_DIR", str(tmp_path))
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    assert evidence_mod._scope_file_basis("no-such-target") is None
    (tmp_path / "broken.yaml").write_text("target: [unterminated", encoding="utf-8")
    assert evidence_mod._scope_file_basis("broken") is None
