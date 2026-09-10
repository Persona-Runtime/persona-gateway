from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is required for migrations")
    return value


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "pyformat"},
        version_table_schema="persona_minimal",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import create_engine

    url = database_url()
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    connectable = create_engine(url, poolclass=None)
    with connectable.connect() as connection:
        # Alembic creates its version table before the first revision runs.
        connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS persona_minimal")
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=None,
            version_table_schema="persona_minimal",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
