"""Built-in ``system:*`` tasks (not tied to a feature module)."""

from __future__ import annotations

import structlog
from procrastinate import Blueprint, JobContext

log = structlog.get_logger(__name__)

blueprint = Blueprint()

#: Finished jobs older than this are removed by ``system:cleanup_jobs``.
JOB_RETENTION_HOURS = 14 * 24


@blueprint.task(name="noop", queue="default")
async def noop(message: str = "ping") -> str:
    """Does nothing but log; used by smoke tests and to verify a running worker."""
    log.info("noop", message=message)
    return message


@blueprint.periodic(cron="17 3 * * *")
@blueprint.task(name="cleanup_jobs", queue="maintenance", pass_context=True)
async def cleanup_jobs(context: JobContext, timestamp: int) -> None:
    """Nightly: delete finished jobs older than the retention window."""
    await context.app.job_manager.delete_old_jobs(
        nb_hours=JOB_RETENTION_HOURS,
        include_failed=True,
        include_cancelled=True,
        include_aborted=True,
    )
    log.info("jobs_cleaned_up", retention_hours=JOB_RETENTION_HOURS, timestamp=timestamp)
