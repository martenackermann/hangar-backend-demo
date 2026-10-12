"""Database administration used by the ``hangar`` CLI and the test-suite."""

from __future__ import annotations

from pathlib import Path

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.db import Base, commit, create_engine, get_sessionmaker
from app.core.discovery import discover, import_models
from app.jobs.schema import apply_job_schema, ensure_job_schema

log = structlog.get_logger("hangar.db")

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
DEFAULT_SEED_ORDER = 100


async def recreate_schema(engine: AsyncEngine) -> None:
    """Drop and recreate ``public`` and create all tables from the models (dev/test only)."""
    import_models()
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)


async def reset_database(url: str | None = None) -> None:
    settings = get_settings()
    engine = create_engine(url or settings.database_url, poolclass=NullPool)
    try:
        await recreate_schema(engine)
    finally:
        await engine.dispose()
    await apply_job_schema()
    log.info("database_reset", tables=sorted(Base.metadata.tables))


async def create_tables() -> None:
    """``create_all`` without dropping anything (bootstrap before the first migration)."""
    import_models()
    engine = create_engine(poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def has_migrations() -> bool:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    return script.get_current_head() is not None


def run_migrations() -> None:
    """``alembic upgrade head``; falls back to ``create_all`` while no migration exists."""
    from alembic import command
    from alembic.config import Config

    if has_migrations():
        config = Config(str(ALEMBIC_INI))
        config.attributes["configure_logger"] = False  # keep structlog's configuration
        command.upgrade(config, "head")
        log.info("migrations_applied")
    else:
        from app.core.aio import run

        log.warning("no_migrations_found_using_create_all")
        run(create_tables())


async def migrate_jobs_schema() -> bool:
    applied = await ensure_job_schema()
    log.info("job_schema", applied=applied)
    return applied


async def run_seeders() -> list[str]:
    """Run every module's ``seed(session)`` ordered by ``SEED_ORDER`` (then name)."""
    seeders = []
    for module, imported in discover("seed"):
        seed = getattr(imported, "seed", None)
        if seed is None:
            continue
        seeders.append((getattr(imported, "SEED_ORDER", DEFAULT_SEED_ORDER), module, seed))
    ran: list[str] = []
    async with get_sessionmaker()() as session:
        for _, module, seed in sorted(seeders, key=lambda item: (item[0], item[1])):
            log.info("seeding", module=module)
            await seed(session)
            await session.flush()
            ran.append(module)
        await commit(session)
    return ran
