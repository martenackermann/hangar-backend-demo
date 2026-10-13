"""Search, files, jobs, demo reset and health endpoints (API.md §3.5, §3.7, §3.9, §3.10)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select, text

from app.core.auth import CurrentUser, require
from app.core.config import get_settings
from app.core.db import SessionDep, commit
from app.core.enums import JobKind, JobStatus
from app.core.errors import NotFoundError, problem_responses
from app.core.files import file_store, verify_signature
from app.core.jobs import enqueue, job_percent
from app.core.models import BackgroundJob, StoredFile, User
from app.core.pagination import csv_enum
from app.core.read_models.refs import user_ref
from app.core.schemas import ApiModel, EntityRef, JobAccepted, ListResponse, UserRef
from app.core.search import SearchResponse, search

router = APIRouter()


# --- search -----------------------------------------------------------------------------------


@router.get(
    "/search",
    response_model=SearchResponse,
    tags=["search"],
    summary="Global search / command palette",
    responses=problem_responses(401, 422),
)
async def global_search(
    session: SessionDep,
    user: CurrentUser,
    q: Annotated[str, Query(min_length=2, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=20)] = 20,
) -> SearchResponse:
    return await search(session, q, user, limit)


# --- files ------------------------------------------------------------------------------------


@router.get(
    "/files/{file_id}/download",
    tags=["files"],
    summary="Download a file via a signed URL",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "File"},
        **problem_responses(403, 404),
    },
)
async def download_file(
    file_id: int,
    session: SessionDep,
    expires: Annotated[int, Query()],
    signature: Annotated[str, Query(min_length=16, max_length=128)],
) -> FileResponse:
    verify_signature(file_id, expires, signature)
    stored = await session.get(StoredFile, file_id)
    if stored is None:
        raise NotFoundError("File not found.")
    path = file_store().path(stored.storage_key)
    if not path.is_file():
        raise NotFoundError("File not found.")
    return FileResponse(
        path,
        media_type=stored.content_type,
        filename=stored.filename,
        headers={"Cache-Control": "private, no-store"},
    )


# --- jobs -------------------------------------------------------------------------------------


class JobProgress(ApiModel):
    processed: int
    total: int | None
    percent: int | None


class BackgroundJobOut(ApiModel):
    id: int
    kind: JobKind
    status: JobStatus
    progress: JobProgress
    message: str | None
    error: str | None
    cancel_requested: bool
    entity: EntityRef | None
    created_by: UserRef | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict | None


def build_job(job: BackgroundJob) -> BackgroundJobOut:
    entity = None
    if job.entity_type and job.entity_id is not None:
        entity = EntityRef.model_validate(
            {
                "type": job.entity_type,
                "id": job.entity_id,
                "label": f"{job.entity_type.replace('_', ' ')} #{job.entity_id}",
            }
        )
    return BackgroundJobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        progress=JobProgress(
            processed=job.progress_processed, total=job.progress_total, percent=job_percent(job)
        ),
        message=job.message,
        error=job.error,
        cancel_requested=job.cancel_requested,
        entity=entity,
        created_by=user_ref(job.creator),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        result=job.result,
    )


@router.get(
    "/jobs",
    response_model=ListResponse[BackgroundJobOut],
    tags=["jobs"],
    summary="Background jobs (running-jobs indicator)",
    responses=problem_responses(401, 422),
)
async def list_jobs(
    session: SessionDep,
    user: CurrentUser,
    mine: Annotated[bool, Query()] = False,
    status: Annotated[list[JobStatus] | None, Depends(csv_enum("status", JobStatus))] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ListResponse[BackgroundJobOut]:
    stmt = (
        select(BackgroundJob)
        .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
        .limit(limit)
    )
    if mine:
        stmt = stmt.where(BackgroundJob.created_by == user.id)
    if status:
        stmt = stmt.where(BackgroundJob.status.in_(status))
    return ListResponse[BackgroundJobOut](
        data=[build_job(j) for j in (await session.scalars(stmt)).all()]
    )


@router.get(
    "/jobs/{job_id}",
    response_model=BackgroundJobOut,
    tags=["jobs"],
    summary="Background job status",
    responses=problem_responses(401, 404),
)
async def get_job(job_id: int, session: SessionDep, _user: CurrentUser) -> BackgroundJobOut:
    job = await session.get(BackgroundJob, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")
    return build_job(job)


# --- demo -------------------------------------------------------------------------------------


@router.post(
    "/demo/reset",
    status_code=202,
    response_model=JobAccepted,
    tags=["demo"],
    summary="Reset demo data (demo mode only)",
    responses=problem_responses(401, 403, 404),
)
async def demo_reset(
    session: SessionDep, user: Annotated[User, Depends(require("demo.reset"))]
) -> JobAccepted:
    if not get_settings().demo_mode:
        raise NotFoundError("Not available outside demo mode.")
    job = await enqueue(
        session, "demo.reset", kind=JobKind.DEMO_RESET, created_by=user, queueing_lock="demo.reset"
    )
    await commit(session)
    return JobAccepted(job_id=job.id)


# --- health -----------------------------------------------------------------------------------


class Healthz(ApiModel):
    status: Literal["ok"] = "ok"


class Readyz(ApiModel):
    status: Literal["ok", "error"]
    db: Literal["ok", "error"]
    queue: Literal["ok", "error"]


@router.get("/healthz", response_model=Healthz, tags=["health"], summary="Process liveness")
async def healthz() -> Healthz:
    return Healthz()


@router.get(
    "/readyz",
    response_model=Readyz,
    tags=["health"],
    summary="Readiness (DB + job queue)",
    responses={503: {"model": Readyz, "description": "Not ready"}},
)
async def readyz(session: SessionDep) -> Readyz | JSONResponse:
    db: Literal["ok", "error"] = "ok"
    queue: Literal["ok", "error"] = "ok"
    try:
        await session.execute(text("SELECT 1"))
        exists = (
            await session.execute(text("SELECT to_regclass('procrastinate_jobs') IS NOT NULL"))
        ).scalar_one()
        queue = "ok" if exists else "error"
    except Exception:  # noqa: BLE001 - reported, not raised
        db, queue = "error", "error"
    body = Readyz(status="ok" if db == queue == "ok" else "error", db=db, queue=queue)
    if body.status != "ok":
        return JSONResponse(body.model_dump(by_alias=True), status_code=503)
    return body
