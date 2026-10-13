"""Core self-registration (tasks, seeder, exporter, demo truncation). Idempotent."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import Column, exists, select

from app.core.config import get_settings
from app.core.db import Base, open_session
from app.core.models import CORE_TABLES, StoredFile
from app.core.timeutil import now_utc

log = structlog.get_logger(__name__)

_done = False

#: Core tables kept on demo reset (reference rows of migration ``0001_core``).
KEEP_ON_RESET = {"deviation_codes"}


def file_reference_columns() -> list[Column[Any]]:
    """Every column, in core or slice tables, with a foreign key to ``stored_files.id``.

    Read from the shared metadata so core never names a slice table.
    """
    return [
        fk.parent
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table.name == StoredFile.__tablename__
    ]


async def files_cleanup(timestamp: int | None = None) -> int:
    """``files.cleanup`` (daily 03:10 UTC): delete expired ``stored_files`` + their bytes.

    Files that a row still references (import originals and error reports, labels,
    protocols, ...) are kept even when they carry an expiry.
    """
    from app.core.files import file_store

    query = select(StoredFile).where(StoredFile.expires_at < now_utc() - timedelta(minutes=1))
    for column in file_reference_columns():
        query = query.where(~exists().where(column == StoredFile.id))
    async with open_session() as session:
        expired = (await session.scalars(query)).all()
        keys = [row.storage_key for row in expired]
        for row in expired:
            await session.delete(row)
        await session.commit()
    # bytes go only after the rows are gone, so a failed commit never orphans a row
    for key in keys:
        file_store().delete(key)
    log.info("files_cleanup", removed=len(keys))
    return len(keys)


def register_core() -> None:
    global _done
    if _done:
        return
    from app.core.demo import demo_reset_nightly, demo_reset_task, table_exporter
    from app.core.notifications.service import register_notification_task
    from app.core.seed import framework
    from app.core.seed.core_seed import seed_core
    from app.jobs.app import register_task

    register_notification_task()
    register_task(
        files_cleanup,
        name="files.cleanup",
        queue="default",
        lock="files.cleanup",
        periodic="10 3 * * *",
    )
    register_task(demo_reset_task, name="demo.reset", queue="default", lock="demo.reset")
    if get_settings().demo_nightly_reset:
        register_task(
            demo_reset_nightly,
            name="demo.reset_nightly",
            queue="default",
            lock="demo.reset",
            periodic="30 * * * *",
        )
    framework.register_seeder(0, seed_core, "core:seed_core")
    framework.register_seed_exporter(
        table_exporter(*[t for t in CORE_TABLES if t != "booking_sandbox_appointments"]),
        "core:tables",
    )
    framework.register_demo_truncate([t for t in CORE_TABLES if t not in KEEP_ON_RESET])
    _done = True
