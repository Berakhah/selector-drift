"""The one place a database URL is read from the environment.

Task 3 predated `config.py` (Task 5) and read `SD_DATABASE_URL` directly. Task 5 Step 4 switches
this to `get_settings().database_url`, so from day one on there is exactly one source of the
URL. The compose password reaches a local `uv run` only through the exported `SD_DATABASE_URL`
environment variable -- it never appears as a literal in this file.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from selector_drift.config import get_settings

engine: Engine = create_engine(get_settings().database_url, pool_pre_ping=True)

Session = sessionmaker(bind=engine)
