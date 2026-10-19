"""The scorer's start-up check: a scorer that cannot act as the scorer must refuse to serve,
loudly, rather than fail only inside Prefect (nothing alerts on a dead scorer). The evidence-mount
half is tested here against a stub session; the database half against real roles in
test_scorer_role.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from testbench.score import ScorerNotReady, check_scorer_access


class _Session:
    """Satisfies the database half: both probes succeed."""

    def execute(self, *_a: Any, **_k: Any) -> None:
        return None

    def scalar(self, *_a: Any, **_k: Any) -> bool:
        return True


@pytest.mark.unit
def test_a_clean_evidence_directory_passes(tmp_path: Path) -> None:
    (tmp_path / "heal-1").mkdir()
    check_scorer_access(_Session(), tmp_path)  # type: ignore[arg-type]


@pytest.mark.unit
def test_a_missing_evidence_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ScorerNotReady, match="evidence root is not a directory"):
        check_scorer_access(_Session(), tmp_path / "absent")  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize("name", ["sessions", "lake", "corpus"])
def test_an_evidence_mount_that_exposes_more_than_evidence_is_refused(
    tmp_path: Path, name: str
) -> None:
    (tmp_path / name).mkdir()
    with pytest.raises(ScorerNotReady, match="exposes more than evidence"):
        check_scorer_access(_Session(), tmp_path)  # type: ignore[arg-type]
