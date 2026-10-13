"""Background job bookkeeping (``background_jobs``; SPEC §2.4 "Background jobs", §9).

Request side::

    job = await enqueue(session, "imports.process", kind=JobKind.IMPORT, created_by=user,
                        entity=("import_job", import_job.id), import_job_id=import_job.id)
    await commit(session)          # the task is deferred after commit with job_id=job.id
    return JobAccepted(job_id=job.id, import_job_id=import_job.id)

Worker side (progress is written in its own short transactions so pollers see it)::

    async def process(job_id: int, import_job_id: int) -> None:
        async with tracked(job_id) as run:
            await run.progress(10, 100, "Reading file")
            ...
            run.result = {"kind": "import", ...}

``notifications.send`` and ``files.cleanup`` do not create job rows (SPEC_RESOLUTIONS #48).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import after_commit, open_session
from app.core.enums import JobKind, JobStatus, Trigger
from app.core.models import BackgroundJob, User
from app.core.schemas import EntityRef
from app.core.timeutil import now_utc

log = structlog.get_logger(__name__)

TERMINAL_JOB_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


async def create_job(
    session: AsyncSession,
    kind: JobKind,
    created_by: User | int | None = None,
    entity: EntityRef | tuple[str, int] | None = None,
    message: str | None = None,
) -> BackgroundJob:
    entity_type, entity_id = (
        (entity.type, entity.id) if isinstance(entity, EntityRef) else entity or (None, None)
    )
    job = BackgroundJob(
        kind=kind,
        status=JobStatus.QUEUED,
        created_by=created_by.id if isinstance(created_by, User) else created_by,
        entity_type=entity_type,
        entity_id=entity_id,
        message=message,
    )
    session.add(job)
    await session.flush()
    return job


async def enqueue(
    session: AsyncSession,
    task_name: str,
    *,
    kind: JobKind,
    created_by: User | int | None = None,
    entity: EntityRef | tuple[str, int] | None = None,
    queueing_lock: str | None = None,
    **task_kwargs: Any,
) -> BackgroundJob:
    """Create the job row and defer ``task_name(job_id=..., **task_kwargs)`` after commit."""
    from app.jobs.app import job_app

    job = await create_job(session, kind, created_by, entity)
    job_id = job.id

    async def _defer() -> None:
        task = job_app.configure_task(task_name, queueing_lock=queueing_lock)
        await task.defer_async(job_id=job_id, **task_kwargs)

    after_commit(session, _defer)
    return job


async def _update(job_id: int, **values: Any) -> None:
    async with open_session() as session:
        await session.execute(
            update(BackgroundJob).where(BackgroundJob.id == job_id).values(**values)
        )
        await session.commit()


async def start_job(job_id: int, message: str | None = None) -> None:
    await _update(job_id, status=JobStatus.RUNNING, started_at=now_utc(), message=message)


async def job_progress(
    job_id: int, processed: int, total: int | None = None, message: str | None = None
) -> None:
    values: dict[str, Any] = {"progress_processed": processed}
    if total is not None:
        values["progress_total"] = total
    if message is not None:
        values["message"] = message[:500]
    await _update(job_id, **values)


async def finish_job(
    job_id: int,
    status: JobStatus,
    result: dict[str, Any] | None = None,
    *,
    error: str | None = None,
    message: str | None = None,
) -> None:
    values: dict[str, Any] = {"status": status, "finished_at": now_utc(), "result": result}
    if error is not None:
        values["error"] = error[:10_000]
    if message is not None:
        values["message"] = message[:500]
    await _update(job_id, **values)


async def is_cancel_requested(job_id: int) -> bool:
    async with open_session() as session:
        job = await session.get(BackgroundJob, job_id)
        return bool(job and job.cancel_requested)


async def request_cancel(session: AsyncSession, job_id: int) -> bool:
    """Ask a queued/running job to stop (``JobRun.cancel_requested()`` turns true).

    Runs in the caller's transaction (no commit). Returns ``False`` when the job does not
    exist or has already finished.
    """
    result = await session.execute(
        update(BackgroundJob)
        .where(
            BackgroundJob.id == job_id,
            BackgroundJob.status.not_in([s.value for s in TERMINAL_JOB_STATUSES]),
        )
        .values(cancel_requested=True)
        .returning(BackgroundJob.id)
    )
    return result.scalar_one_or_none() is not None


async def record_job(
    session: AsyncSession,
    kind: JobKind,
    status: JobStatus,
    *,
    created_by: User | int | None = None,
    entity: EntityRef | tuple[str, int] | None = None,
    message: str | None = None,
    result: dict[str, Any] | None = None,
    progress: tuple[int, int | None] = (0, None),
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    cancel_requested: bool = False,
) -> BackgroundJob:
    """Insert a job row with its whole history at once (seeders: past imports, syncs, ...).

    Nothing is deferred: the row only documents work that is already finished (or a queued
    job a seeder wants to show).
    """
    job = await create_job(session, kind, created_by, entity, message)
    job.status = status
    job.result = result
    job.progress_processed, job.progress_total = progress
    job.started_at = started_at
    job.finished_at = finished_at
    job.cancel_requested = cancel_requested
    if created_at is not None:
        job.created_at = created_at
        job.updated_at = finished_at or started_at or created_at
    await session.flush()
    return job


class JobRun:
    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        self.result: dict[str, Any] | None = None
        self.status: JobStatus = JobStatus.SUCCEEDED
        self.message: str | None = None

    async def progress(
        self, processed: int, total: int | None = None, message: str | None = None
    ) -> None:
        await job_progress(self.job_id, processed, total, message)

    async def cancel_requested(self) -> bool:
        return await is_cancel_requested(self.job_id)


@asynccontextmanager
async def tracked(job_id: int, *, trigger: Trigger = Trigger.JOB) -> AsyncIterator[JobRun]:
    """Mark the job running; on exit ``succeeded`` (or ``run.status``) / ``failed``."""
    from app.core.context import job_context

    run = JobRun(job_id)
    await start_job(job_id)
    with job_context(trigger):
        try:
            yield run
        except Exception as exc:
            log.exception("job_failed", job_id=job_id)
            await finish_job(
                job_id, JobStatus.FAILED, run.result, error=f"{type(exc).__name__}: {exc}"
            )
            raise
    await finish_job(job_id, run.status, run.result, message=run.message)


def job_percent(job: BackgroundJob) -> int | None:
    if not job.progress_total:
        return 100 if job.status == JobStatus.SUCCEEDED else None
    return min(100, round(job.progress_processed * 100 / job.progress_total))
