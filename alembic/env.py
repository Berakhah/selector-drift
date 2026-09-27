"""Alembic environment. Reads the database URL the same way `selector_drift.db.session` does
-- `SD_DATABASE_URL`, with the same password-less default -- so there is exactly one source of
the URL from day one. Task 5 Step 4 switches `session.py` to `get_settings().database_url`;
this file follows suit at the same time so the two never diverge.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from selector_drift.db.models import Base

# this is the Alembic Config object, which provides access to the values within the .ini file.
config = context.config

# Interpret the config file for Python logging. This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_DEFAULT_DATABASE_URL = "postgresql+psycopg://selector_drift@localhost:5432/selector_drift"


def _database_url() -> str:
    return os.environ.get("SD_DATABASE_URL", _DEFAULT_DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode -- emits SQL to a script rather than a live
    connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode -- against a live connection."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
