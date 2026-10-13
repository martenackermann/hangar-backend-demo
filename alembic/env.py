"""Alembic environment: async engine; metadata = core models + every slice ``models.py``.

procrastinate's tables are managed by ``hangar db upgrade`` (schema apply) and excluded.
Slices add revisions on their own branch (CONVENTIONS.md "Migrations").
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any, Literal

import sqlalchemy as sa
from alembic import context
from alembic.operations import ops
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import conv

from app.core.aio import run
from app.core.config import get_settings
from app.core.db import Base, create_engine
from app.core.slices import import_models

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

import_models()
target_metadata = Base.metadata

EXCLUDED_PREFIXES = ("procrastinate_",)


def include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    if type_ == "table" and name is not None:
        return not name.startswith(EXCLUDED_PREFIXES)
    # expression (trigram) indexes cannot be compared reliably; they are written by hand
    return not (type_ == "index" and name is not None and name.endswith("_trgm"))


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    return not (type_ == "index" and name is not None and name.endswith("_trgm"))


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | Literal[False]:
    """Render non-native enums without their own CHECK (the table carries one explicit,
    convention-named CHECK instead — see :func:`process_revision_directives`)."""
    if type_ == "type" and isinstance(obj, sa.Enum) and not obj.native_enum:
        values = ", ".join(repr(v) for v in obj.enums)
        return f"sa.Enum({values}, native_enum=False, create_constraint=False, length={obj.length})"
    return False


def _enum_check_sql(column: sa.Column[Any]) -> str:
    values = ", ".join("'" + v.replace("'", "''") + "'" for v in column.type.enums)  # type: ignore[attr-defined]
    return f"{column.name} IN ({values})"


def process_revision_directives(context_: Any, revision: Any, directives: list[Any]) -> None:
    """One CHECK per non-native enum column, named ``ck_<table>_<column>`` (SPEC §0)."""
    script = directives[0]
    for container in (script.upgrade_ops,):
        new_ops = []
        for op_ in container.ops:
            if isinstance(op_, ops.CreateTableOp):
                columns = [
                    c
                    for c in op_.columns
                    if not isinstance(c, sa.CheckConstraint) or _keep_check(c)
                ]
                for column in [c for c in op_.columns if isinstance(c, sa.Column)]:
                    if isinstance(column.type, sa.Enum) and not column.type.native_enum:
                        columns.append(
                            sa.CheckConstraint(
                                _enum_check_sql(column),
                                name=conv(f"ck_{op_.table_name}_{column.name}"),
                            )
                        )
                        column.type = _plain_enum(column.type)
                op_.columns = columns
                new_ops.append(op_)
            elif isinstance(op_, ops.ModifyTableOps):
                inner = []
                for sub in op_.ops:
                    inner.append(sub)
                    if not isinstance(sub, ops.AddColumnOp):
                        continue
                    column = sub.column
                    if isinstance(column.type, sa.Enum) and not column.type.native_enum:
                        column.type = _plain_enum(column.type)
                        inner.append(
                            ops.CreateCheckConstraintOp(
                                conv(f"ck_{op_.table_name}_{column.name}"),
                                op_.table_name,
                                sa.text(_enum_check_sql(column)),
                            )
                        )
                op_.ops = inner
                new_ops.append(op_)
            else:
                new_ops.append(op_)
        container.ops = new_ops


def _plain_enum(type_: sa.Enum) -> sa.Enum:
    return sa.Enum(*type_.enums, native_enum=False, create_constraint=False, length=type_.length)


def _keep_check(constraint: sa.CheckConstraint) -> bool:
    """Drop the enum-generated CHECKs (re-added with convention names); keep named ones."""
    name = constraint.name
    if not isinstance(name, str):
        return False  # unnamed enum CHECK
    return name.startswith("ck_")


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_name=include_name,
        include_object=include_object,
        render_item=render_item,
        process_revision_directives=process_revision_directives,
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(
        url=get_settings().database_url, literal_binds=True, dialect_opts={"paramstyle": "named"}
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
