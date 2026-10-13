"""Database administration for the CLI and the test-suite (SPEC §2.6)."""

from __future__ import annotations

from pathlib import Path

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.db import Base, create_engine
from app.core.slices import import_models
from app.jobs.schema import apply_job_schema, ensure_job_schema

log = structlog.get_logger("hangar.db")

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


async def recreate_schema(engine: AsyncEngine) -> None:
    """Drop + recreate ``public``, enable ``pg_trgm`` and ``create_all`` (dev/test only)."""
    import_models()
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)


async def reset_database(url: str | None = None) -> None:
    engine = create_engine(url or get_settings().database_url, poolclass=NullPool)
    try:
        await recreate_schema(engine)
    finally:
        await engine.dispose()
    await apply_job_schema()
    log.info("database_reset", tables=len(Base.metadata.tables))


def alembic_config(*, configure_logger: bool = False):
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.attributes["configure_logger"] = configure_logger
    return config


def upgrade_heads() -> None:
    """``alembic upgrade heads`` (works before and after the slice-branch merge revision)."""
    from alembic import command

    command.upgrade(alembic_config(), "heads")
    log.info("migrations_applied")


async def migrate_jobs_schema() -> bool:
    applied = await ensure_job_schema()
    log.info("job_schema", applied=applied)
    return applied
