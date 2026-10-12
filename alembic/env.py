"""Alembic environment: async engine, metadata from every ``app/modules/*/models.py``.

procrastinate's tables/types are managed by ``hangar jobs-schema`` and excluded here.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection

from app.core.aio import run
from app.core.config import get_settings
from app.core.db import Base, create_engine
from app.core.discovery import import_models

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

import_models()
target_metadata = Base.metadata

EXCLUDED_PREFIXES = ("procrastinate_",)


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    if type_ == "table" and name is not None:
        return not name.startswith(EXCLUDED_PREFIXES)
    return True


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_name=include_name,
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(
        url=get_settings().database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_engine(poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # a caller (e.g. tests) passed a sync connection
        do_run_migrations(connection)
    else:
        run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
