"""Transaction runtimes for the import processor.

* :class:`TaskRuntime` (worker): one DB transaction per chunk (committed, or rolled back for
  dry runs), bookkeeping in its own short transactions, progress via the core job helpers.
* :class:`SessionRuntime` (tests, synchronous use): everything on one session; chunks are
  SAVEPOINTs, bookkeeping is flushed. Task code that opens its own sessions cannot see a
  test transaction, so tests drive the processor through this runtime (CONVENTIONS §3.9).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import commit, open_session
from app.core.jobs import job_progress

from ..models import ImportJob


class ImportRuntime(Protocol):
    def chunk(self, *, rollback: bool) -> AbstractAsyncContextManager[AsyncSession]: ...
    def meta(self) -> AbstractAsyncContextManager[AsyncSession]: ...
    async def progress(self, processed: int, total: int | None, message: str) -> None: ...
    async def cancel_requested(self, import_job_id: int) -> bool: ...


class TaskRuntime:
    def __init__(self, job_id: int | None) -> None:
        self.job_id = job_id

    @asynccontextmanager
    async def chunk(self, *, rollback: bool) -> AsyncIterator[AsyncSession]:
        async with open_session() as session:
            yield session
            if rollback:
                await session.rollback()
            else:
                await commit(session)

    @asynccontextmanager
    async def meta(self) -> AsyncIterator[AsyncSession]:
        async with open_session() as session:
            yield session
            await commit(session)

    async def progress(self, processed: int, total: int | None, message: str) -> None:
        if self.job_id is not None:
            await job_progress(self.job_id, processed, total, message)

    async def cancel_requested(self, import_job_id: int) -> bool:
        async with open_session() as session:
            return bool(
                await session.scalar(
                    select(ImportJob.cancel_requested).where(ImportJob.id == import_job_id)
                )
            )


class SessionRuntime:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.progress_log: list[tuple[int, int | None, str]] = []

    @asynccontextmanager
    async def chunk(self, *, rollback: bool) -> AsyncIterator[AsyncSession]:
        savepoint = await self.session.begin_nested()
        try:
            yield self.session
        except BaseException:
            await savepoint.rollback()
            raise
        if rollback:
            await savepoint.rollback()
        else:
            await savepoint.commit()

    @asynccontextmanager
    async def meta(self) -> AsyncIterator[AsyncSession]:
        yield self.session
        await self.session.flush()

    async def progress(self, processed: int, total: int | None, message: str) -> None:
        self.progress_log.append((processed, total, message))

    async def cancel_requested(self, import_job_id: int) -> bool:
        return bool(
            await self.session.scalar(
                select(ImportJob.cancel_requested).where(ImportJob.id == import_job_id)
            )
        )
