"""The one place a database URL is read from the environment.

Task 3 predates `config.py` (Task 5): this module reads `SD_DATABASE_URL` directly, with a
password-less default matching the one Task 5's `config.py` will use. Task 5 Step 4 switches
this to `get_settings().database_url`, so from day one on there is exactly one source of the
URL. The compose password reaches a local `uv run` only through the exported `SD_DATABASE_URL`
environment variable -- it never appears as a literal in this file.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

_DEFAULT_DATABASE_URL = "postgresql+psycopg://selector_drift@localhost:5432/selector_drift"


def _database_url() -> str:
    return os.environ.get("SD_DATABASE_URL", _DEFAULT_DATABASE_URL)


engine: Engine = create_engine(_database_url())

Session = sessionmaker(bind=engine)
