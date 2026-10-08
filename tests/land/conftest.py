from collections.abc import Iterator
from pathlib import Path

import pytest

from selector_drift.config import get_settings
from selector_drift.extract.models import CandidateExtraction
from tests.land.factory import candidate as make_candidate
from tests.land.factory import configure_lake, rows


@pytest.fixture
def lake_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield configure_lake(tmp_path, monkeypatch)
    get_settings.cache_clear()


@pytest.fixture
def candidate() -> CandidateExtraction:
    return make_candidate(rows(3))
