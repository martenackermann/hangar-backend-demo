"""Chunked import processing (SPEC §7.1 "Processing", "Dry run", "Post-processing").

Polars reads and validates the whole file once; valid rows are applied in chunks of 500
(one transaction per chunk, one SAVEPOINT per row). After each chunk the counts, issues
and job progress are written in a separate short transaction and ``cancel_requested`` is
checked. Post-processing runs once in its own transaction; finally the job is finished,
the failed-row report stored, ``import.summary`` queued (errors > 0) and
``ImportFinished`` emitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import structlog
from sqlalchemy.exc import SQLAlchemyError

from app.core import db as core_db
from app.core.config import get_settings
from app.core.enums import ImportJobStatus, ImportType
from app.core.errors import ApiError
from app.core.events import ImportFinished, emit
from app.core.files import read_file, store_file
from app.core.models import User
from app.core.notifications import notify
from app.core.schemas import EntityRef
from app.core.timeutil import now_utc

from ..models import ImportJob, ImportJobError, empty_post_process
from .appliers import Applier, applier_for
from .context import ApplyContext, JobStats, Outcome, RowEffects
from .issues import FileProblem, Issue, Row, RowError, truncate
from .reader import read_table
from .report import build_error_report
from .runtime import ImportRuntime
from .validate import Validation, validate_table

log = structlog.get_logger(__name__)

CHUNK_SIZE = 500
TERMINAL = frozenset(
    {
        ImportJobStatus.COMPLETED,
        ImportJobStatus.COMPLETED_WITH_ERRORS,
        ImportJobStatus.FAILED,
        ImportJobStatus.CANCELLED,
    }
)
_AFTER_COMMIT_KEY: str = getattr(core_db, "_AFTER_COMMIT_KEY", "hangar.after_commit")


@dataclass(slots=True)
class ImportResult:
    import_job_id: int
    status: ImportJobStatus
    counts: dict[str, Any]
    post_process: dict[str, Any] = field(default_factory=dict)


def job_counts(job: ImportJob) -> dict[str, Any]:
    return {
        "total": job.total_rows,
        "processed": job.processed_rows,
        "created": job.created_count,
        "updated": job.updated_count,
        "unchanged": job.unchanged_count,
        "skipped": job.skipped_count,
        "errors": job.error_count,
        "warnings": job.warning_count,
    }


def _store_counts(job: ImportJob, stats: JobStats) -> None:
    job.processed_rows = stats.processed
    job.created_count = stats.created
    job.updated_count = stats.updated
    job.unchanged_count = stats.unchanged
    job.skipped_count = stats.skipped
    job.error_count = stats.errors
    job.warning_count = stats.warnings


def _error_rows(import_job_id: int, rows: list[Row]) -> list[ImportJobError]:
    return [
        ImportJobError(
            import_job_id=import_job_id,
            row_number=row.number,
            severity=issue.severity,
            column_name=truncate(issue.column, 64),
            code=issue.code[:48],
            message=truncate(issue.message, 500) or "",
            raw_value=truncate(issue.raw_value, 500),
            business_key=truncate(row.key, 40),
        )
        for row in rows
        for issue in row.issues
    ]


async def _apply_row(ctx: ApplyContext, applier: Applier, row: Row, stats: JobStats) -> None:
    if row.has_errors:
        stats.add_row(row, None, None)
        return
    session = ctx.session
    effects = RowEffects()
    pending: list[Any] = session.info.setdefault(_AFTER_COMMIT_KEY, [])
    callbacks_before = len(pending)
    savepoint = await session.begin_nested()
    failure: Issue | None = None
    outcome: Outcome | None = None
    try:
        outcome = await applier.apply(ctx, row, effects)
        await session.flush()
    except RowError as exc:
        failure = exc.issue
    except ApiError as exc:
        failure = Issue.error(exc.code, exc.detail or str(exc))
    except SQLAlchemyError as exc:
        log.warning("import_row_failed", row=row.number, error=str(exc))
        failure = Issue.error("row_failed", "The row could not be saved (database constraint).")
    if failure is None:
        await savepoint.commit()
        row.issues.extend(effects.warnings)
        stats.add_row(row, outcome, effects)
        return
    await savepoint.rollback()
    # mails/jobs queued by the rolled-back row must not be sent
    del session.info.setdefault(_AFTER_COMMIT_KEY, [])[callbacks_before:]
    for user_id in effects.contacts_created:
        ctx.forget_contact(user_id)
    row.issues.append(failure)
    stats.add_row(row, None, None)


async def run_import(runtime: ImportRuntime, import_job_id: int) -> ImportResult:
    """Process one import job end to end (idempotent: terminal jobs are left alone)."""
    async with runtime.meta() as session:
        job = await session.get(ImportJob, import_job_id, populate_existing=True)
        if job is None:
            raise LookupError(f"Import job {import_job_id} not found")
        if job.status in TERMINAL:
            return ImportResult(job.id, job.status, job_counts(job), dict(job.post_process))
        if job.cancel_requested:
            return await _finish(runtime, import_job_id, ImportJobStatus.CANCELLED)
        job.status = ImportJobStatus.PROCESSING
        job.started_at = now_utc()
        data = read_file(job.file)
        extension = PurePosixPath(job.original_filename).suffix.lower()
        import_type = ImportType(job.type)
        options = dict(job.options or {})
        actor_id = job.created_by
    await runtime.progress(0, None, "Reading file")

    try:
        table = read_table(data, extension, max_rows=get_settings().import_max_rows)
        validation = validate_table(table, import_type)
    except FileProblem as exc:
        return await _finish(runtime, import_job_id, ImportJobStatus.FAILED, failure=exc.message)

    rows = validation.rows
    total = len(rows)
    async with runtime.meta() as session:
        job = await session.get(ImportJob, import_job_id, populate_existing=True)
        assert job is not None
        job.total_rows = total
        job.job_warnings = validation.job_warnings
    await runtime.progress(0, total, f"Validated {total} rows")

    ctx = ApplyContext(import_job_id, import_type, options, actor_id, now_utc())
    applier = applier_for(import_type)
    stats = JobStats()
    seen_keys = {row.key for row in rows if row.key}
    cancelled = False
    post: dict[str, Any] = empty_post_process()
    try:
        for start in range(0, total, CHUNK_SIZE):
            chunk = rows[start : start + CHUNK_SIZE]
            async with runtime.chunk(rollback=ctx.dry_run) as session:
                ctx.session = session
                for row in chunk:
                    await _apply_row(ctx, applier, row, stats)
            async with runtime.meta() as session:
                session.add_all(_error_rows(import_job_id, chunk))
                job = await session.get(ImportJob, import_job_id, populate_existing=True)
                assert job is not None
                _store_counts(job, stats)
            await runtime.progress(
                stats.processed, total, f"Processed {stats.processed} of {total} rows"
            )
            if start + CHUNK_SIZE < total and await runtime.cancel_requested(import_job_id):
                cancelled = True
                break
        if not cancelled and await runtime.cancel_requested(import_job_id):
            cancelled = stats.processed < total
        async with runtime.chunk(rollback=ctx.dry_run) as session:
            ctx.session = session
            post.update(
                await applier.post_process(ctx, stats, seen_keys=seen_keys, complete=not cancelled)
            )
    except Exception as exc:
        log.exception("import_failed", import_job_id=import_job_id)
        return await _finish(
            runtime,
            import_job_id,
            ImportJobStatus.FAILED,
            stats=stats,
            validation=validation,
            post=post,
            failure=f"Unexpected error: {type(exc).__name__}: {exc}"[:500],
        )

    if cancelled:
        status = ImportJobStatus.CANCELLED
    elif total and stats.errors >= total:
        status = ImportJobStatus.FAILED
    elif stats.errors:
        status = ImportJobStatus.COMPLETED_WITH_ERRORS
    else:
        status = ImportJobStatus.COMPLETED
    failure = "Every row has errors; nothing was imported." if status == "failed" else None
    return await _finish(
        runtime,
        import_job_id,
        status,
        stats=stats,
        validation=validation,
        post=post,
        failure=failure,
    )


async def _finish(
    runtime: ImportRuntime,
    import_job_id: int,
    status: ImportJobStatus,
    *,
    stats: JobStats | None = None,
    validation: Validation | None = None,
    post: dict[str, Any] | None = None,
    failure: str | None = None,
) -> ImportResult:
    async with runtime.meta() as session:
        job = await session.get(ImportJob, import_job_id, populate_existing=True)
        assert job is not None
        job.status = status
        job.finished_at = now_utc()
        job.started_at = job.started_at or job.finished_at
        if stats is not None:
            _store_counts(job, stats)
        if post is not None:
            job.post_process = {**empty_post_process(), **post}
        if failure:
            job.failure = failure[:500]
        if validation is not None:
            issues = [(row.number, issue) for row in validation.rows for issue in row.issues]
            if issues:
                report = build_error_report(
                    validation.original_headers, validation.original, issues
                )
                stored = await store_file(
                    session,
                    report,
                    f"import-{job.id}-issues.csv",
                    "text/csv",
                    "import_error_report",
                    entity=("import_job", job.id),
                    created_by=job.created_by,
                )
                job.error_report_file_id = stored.id
        await session.flush()
        if job.error_count > 0 and not job.dry_run:
            await _notify_summary(session, job)
        if not job.dry_run:
            await emit(session, ImportFinished(job.id, ImportType(job.type), status))
        result = ImportResult(job.id, status, job_counts(job), dict(job.post_process))
    log.info("import_finished", import_job_id=import_job_id, status=str(status))
    return result


async def _notify_summary(session: Any, job: ImportJob) -> None:
    creator = await session.get(User, job.created_by)
    if creator is None or not creator.email:
        return
    settings = get_settings()
    await notify(
        session,
        "import.summary",
        to=[creator.email],
        context=summary_context(job, creator, settings.frontend_url),
        entity=EntityRef(type="import_job", id=job.id, label=f"Import #{job.id}"),
        attachments=[job.error_report_file_id] if job.error_report_file_id else [],
        actor=creator,
    )


def summary_context(job: ImportJob, creator: User, frontend_url: str) -> dict[str, Any]:
    from app.core.enums import label

    return {
        "import_id": job.id,
        "import_type": str(job.type),
        "type_label": label(ImportType(job.type)),
        "status_label": label(ImportJobStatus(job.status)),
        "file_name": job.original_filename,
        "first_name": creator.first_name,
        "total": job.total_rows or 0,
        "created": job.created_count,
        "updated": job.updated_count,
        "unchanged": job.unchanged_count,
        "skipped": job.skipped_count,
        "error_count": job.error_count,
        "warning_count": job.warning_count,
        "detail_url": f"{frontend_url}/data/imports/{job.id}",
        "has_report": job.error_report_file_id is not None,
    }
