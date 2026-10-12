"""``GET /api/v1/jobs/{jobId}`` — status of a background job (for ``202 {jobId}`` flows)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter

from app.core.errors import NotFoundError, problem_responses
from app.core.schemas import ApiModel
from app.core.security import CurrentUserDep
from app.jobs.app import job_app

router = APIRouter(tags=["jobs"])

type JobStatus = Literal["todo", "doing", "succeeded", "failed", "cancelled", "aborting", "aborted"]


class JobOut(ApiModel):
    id: int
    task: str
    queue: str
    status: JobStatus
    attempts: int
    scheduled_at: datetime | None = None
    finished: bool


_FINISHED = {"succeeded", "failed", "cancelled", "aborted"}


@router.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    summary="Background job status",
    responses=problem_responses(401, 404),
)
async def get_job(job_id: int, _user: CurrentUserDep) -> JobOut:
    jobs = list(await job_app.job_manager.list_jobs_async(id=job_id))
    if not jobs:
        raise NotFoundError(f"Job {job_id} not found.")
    job = jobs[0]
    status = job.status or "todo"
    return JobOut(
        id=job_id,
        task=job.task_name,
        queue=job.queue,
        status=status,  # pyright: ignore[reportArgumentType]
        attempts=job.attempts,
        scheduled_at=job.scheduled_at,
        finished=status in _FINISHED,
    )
