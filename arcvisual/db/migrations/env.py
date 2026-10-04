"""Alembic environment. Migrations exist from commit one — the production schema
is never hand-edited, which is what makes ``pipeline_version`` rollback safe."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from arcvisual.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The DSN comes from the environment, never from alembic.ini, so a connection
# string is never committed.
dsn = os.environ.get("DATABASE_URL")
if dsn and not config.get_main_option("sqlalchemy.url"):
    from arcvisual.db.repo import normalize_dsn

    # Hosted Postgres hands out bare postgresql:// URLs, which SQLAlchemy reads as
    # psycopg2 — not installed. Pin psycopg 3, exactly as the application engine does.
    config.set_main_option("sqlalchemy.url", normalize_dsn(dsn).replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A caller that already holds a connection (and the advisory lock on it) hands
    # it over here — see arcvisual.db.migrate.ensure_schema.
    supplied = config.attributes.get("connection")
    if supplied is not None:
        context.configure(
            connection=supplied, target_metadata=target_metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
