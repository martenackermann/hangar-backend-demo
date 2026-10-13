"""Worker tasks: ``imports.process`` (queue ``imports``) and ``regions.reapply`` (SPEC §9)."""

from __future__ import annotations

from app.core.db import commit, open_session
from app.core.enums import ImportJobStatus, JobStatus, Trigger
from app.core.jobs import tracked

from .importing.processor import run_import
from .importing.runtime import TaskRuntime
from .services.regions import reapply_regions

_JOB_STATUS = {
    ImportJobStatus.COMPLETED: JobStatus.SUCCEEDED,
    ImportJobStatus.COMPLETED_WITH_ERRORS: JobStatus.SUCCEEDED,
    ImportJobStatus.FAILED: JobStatus.FAILED,
    ImportJobStatus.CANCELLED: JobStatus.CANCELLED,
}


async def process_import(job_id: int, import_job_id: int) -> None:
    """Task body of ``imports.process``; the testable core is ``run_import``."""
    async with tracked(job_id, trigger=Trigger.IMPORT) as run:
        result = await run_import(TaskRuntime(job_id), import_job_id)
        run.status = _JOB_STATUS.get(result.status, JobStatus.SUCCEEDED)
        run.message = f"Import {result.status}"
        run.result = {
            "kind": "import",
            "importJobId": import_job_id,
            "status": result.status.value,
            "counts": result.counts,
        }


async def reapply_regions_task(job_id: int) -> None:
    """Task body of ``regions.reapply`` (one transaction; progress in separate ones)."""
    async with tracked(job_id, trigger=Trigger.JOB) as run, open_session() as session:
        run.result = await reapply_regions(session, run.progress)
        await commit(session)
