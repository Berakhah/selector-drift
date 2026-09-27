"""Alembic environment. Reads the database URL through `selector_drift.config.get_settings()`,
the one place configuration is read (M-2, final review): `db/session.py` already reads
`database_url` from there, and this file used to keep its own `SD_DATABASE_URL` reader with its
own default, which disagreed with `Settings`' default and meant `SD_DATABASE_URL=` (set but
empty, exactly what a bare `KEY=` line in `.env.example` produces once sourced) crashed alembic
while `session.py` fell back cleanly. One reader now, so the two can never diverge.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from selector_drift.config import get_settings
from selector_drift.db.models import Base

# this is the Alembic Config object, which provides access to the values within the .ini file.
config = context.config

# Interpret the config file for Python logging. This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return get_settings().database_url


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
