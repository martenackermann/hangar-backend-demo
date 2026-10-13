"""Core self-registration (tasks, seeder, exporter, demo truncation). Idempotent."""

from __future__ import annotations

from datetime import timedelta

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import open_session
from app.core.models import CORE_TABLES, StoredFile
from app.core.timeutil import now_utc

log = structlog.get_logger(__name__)

_done = False

#: Core tables kept on demo reset (reference rows of migration ``0001_core``).
KEEP_ON_RESET = {"deviation_codes"}


async def files_cleanup(timestamp: int | None = None) -> int:
    """``files.cleanup`` (daily 03:10 UTC): delete expired ``stored_files`` + their bytes."""
    from app.core.files import file_store

    removed = 0
    async with open_session() as session:
        expired = (
            await session.scalars(
                select(StoredFile).where(StoredFile.expires_at < now_utc() - timedelta(minutes=1))
            )
        ).all()
        for row in expired:
            file_store().delete(row.storage_key)
            await session.delete(row)
            removed += 1
        await session.commit()
    log.info("files_cleanup", removed=removed)
    return removed


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
