"""procrastinate schema bootstrap (equivalent of ``procrastinate schema --apply``)."""

from __future__ import annotations

import psycopg
from procrastinate.schema import SchemaManager

from app.core.config import get_settings


async def job_schema_exists(conninfo: str | None = None) -> bool:
    async with await psycopg.AsyncConnection.connect(
        conninfo or get_settings().psycopg_conninfo
    ) as conn:
        cursor = await conn.execute("SELECT to_regclass('procrastinate_jobs') IS NOT NULL")
        row = await cursor.fetchone()
        return bool(row and row[0])


async def apply_job_schema(conninfo: str | None = None) -> None:
    async with await psycopg.AsyncConnection.connect(
        conninfo or get_settings().psycopg_conninfo, autocommit=True
    ) as conn:
        await conn.execute(SchemaManager.get_schema())


async def ensure_job_schema(conninfo: str | None = None) -> bool:
    """Apply the schema if it is missing. Returns ``True`` when it was applied."""
    if await job_schema_exists(conninfo):
        return False
    await apply_job_schema(conninfo)
    return True
