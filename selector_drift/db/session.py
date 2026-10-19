"""The engine and sessionmaker, built from `config.get_settings()`.

`selector_drift.config` is the one place a database URL (or any other setting) is read from the
environment (fix round 1, minor 3). Task 3 predated `config.py` (Task 5) and read
`SD_DATABASE_URL` directly with its own default; Task 5 Step 4 replaced that with
`get_settings().database_url`, so this module no longer reads the environment itself. The
compose password reaches a local `uv run` only through the exported `SD_DATABASE_URL`
environment variable -- it never appears as a literal in this file.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from selector_drift.config import get_settings

engine: Engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    hide_parameters=True,  # a bound argon2 hash must never print in a traceback
)

Session = sessionmaker(bind=engine)
