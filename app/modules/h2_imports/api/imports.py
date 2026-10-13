"""``/imports`` (API.md §5.1)."""

import random
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require
from app.core.config import get_settings
from app.core.db import SessionDep, commit
from app.core.enums import ImportJobStatus, ImportType
from app.core.errors import NotFoundError, problem_responses
from app.core.files import SPREADSHEETS, read_upload, signed_url, store_file
from app.core.gateways import TicketingDep
from app.core.models import ServiceRequest, User
from app.core.pagination import (
    OrderBy,
    Page,
    PageParamsDep,
    csv_enum,
    fetch_page,
    sort_param,
)
from app.core.schemas import DownloadLink, JobAccepted, ListResponse
from app.core.timeutil import business_datetime

from ..models import ImportJob, ImportJobError
from ..samples import DEFAULT_SAMPLE, build_sample
from ..schemas import (
    EmptyBody,
    GenerateSampleIn,
    ImportDetection,
    ImportJobDetail,
    ImportJobSummary,
    ImportRowIssue,
    ImportTemplate,
)
from ..services import imports as service

router = APIRouter(prefix="/imports", tags=["imports"])

SORT = sort_param(
    {"createdAt": ImportJob.created_at, "finishedAt": ImportJob.finished_at},
    default="-createdAt",
    tiebreaker=ImportJob.id,
)
_READ = Depends(require("imports.read"))
_CREATE = require("imports.create")


async def _get(session: AsyncSession, import_id: int) -> ImportJob:
    job = await session.get(ImportJob, import_id)
    if job is None:
        raise NotFoundError(f"Import {import_id} not found.")
    return job


@router.post(
    "",
    status_code=202,
    response_model=JobAccepted,
    summary="Upload an import (CSV/XLSX); the type is detected from the headers",
    responses=problem_responses(400, 401, 403, 413, 415, 422),
)
async def upload_import(
    session: SessionDep,
    user: Annotated[User, Depends(_CREATE)],
    file: Annotated[UploadFile, File(description="CSV (UTF-8) or XLSX file")],
    type: Annotated[ImportType | None, Form(description="Force the import type")] = None,
    options: Annotated[str | None, Form(description="ImportOptions as a JSON string")] = None,
) -> JobAccepted:
    settings = get_settings()
    upload = await read_upload(file, allowed=SPREADSHEETS, max_bytes=settings.import_max_bytes)
    job = await service.create_import(
        session,
        user,
        upload,
        forced_type=type,
        options_raw=options,
        max_rows=settings.import_max_rows,
    )
    background_job_id = job.background_job_id
    import_job_id = job.id
    await commit(session)
    return JobAccepted(job_id=background_job_id, import_job_id=import_job_id)


@router.post(
    "/detect",
    response_model=ImportDetection,
    summary="Detect the import type and preview a file (nothing is stored)",
    responses=problem_responses(401, 403, 413, 415),
)
async def detect_import(
    _user: Annotated[User, Depends(_CREATE)],
    file: Annotated[UploadFile, File(description="CSV (UTF-8) or XLSX file")],
) -> ImportDetection:
    settings = get_settings()
    upload = await read_upload(file, allowed=SPREADSHEETS, max_bytes=settings.import_max_bytes)
    return service.detect(upload, settings.import_max_rows)


@router.get(
    "",
    response_model=Page[ImportJobSummary],
    summary="Import history",
    dependencies=[_READ],
    responses=problem_responses(401, 403, 422),
)
async def list_imports(
    session: SessionDep,
    user: CurrentUser,
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
    types: Annotated[list[ImportType] | None, Depends(csv_enum("type", ImportType))] = None,
    statuses: Annotated[
        list[ImportJobStatus] | None, Depends(csv_enum("status", ImportJobStatus))
    ] = None,
    created_by_id: Annotated[int | None, Query(alias="createdById")] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> Page[ImportJobSummary]:
    stmt = select(ImportJob).order_by(*order)
    if types:
        stmt = stmt.where(ImportJob.type.in_(types))
    if statuses:
        stmt = stmt.where(ImportJob.status.in_(statuses))
    if created_by_id is not None:
        stmt = stmt.where(ImportJob.created_by == created_by_id)
    if date_from is not None:
        stmt = stmt.where(ImportJob.created_at >= business_datetime(date_from))
    if date_to is not None:
        stmt = stmt.where(ImportJob.created_at < business_datetime(date_to + timedelta(days=1)))
    rows, meta = await fetch_page(session, stmt, paging)
    return Page[ImportJobSummary](data=[service.build_summary(j, user) for j in rows], meta=meta)


@router.get(
    "/templates",
    response_model=ListResponse[ImportTemplate],
    summary="Column specifications of every import type",
    dependencies=[_READ],
    responses=problem_responses(401, 403),
)
async def list_templates() -> ListResponse[ImportTemplate]:
    return ListResponse[ImportTemplate](data=service.templates())


@router.get(
    "/templates/{import_type}/sample",
    response_model=DownloadLink,
    summary="Download the sample file of an import type",
    responses=problem_responses(401, 403, 404, 422),
)
async def download_sample(
    import_type: ImportType,
    session: SessionDep,
    user: Annotated[User, Depends(require("imports.read"))],
) -> DownloadLink:
    name = DEFAULT_SAMPLE[import_type]
    data = await build_sample(session, name)
    stored = await store_file(
        session,
        data,
        f"{name}.csv",
        "text/csv",
        "sample",
        ttl=service.SAMPLE_TTL,
        created_by=user,
    )
    link = signed_url(stored)
    await commit(session)
    return link


@router.post(
    "/generate-sample",
    response_model=DownloadLink,
    summary="Generate a fresh ITSM export (demo mode)",
    responses=problem_responses(401, 403, 404, 422),
)
async def generate_sample(
    body: GenerateSampleIn,
    session: SessionDep,
    user: Annotated[User, Depends(_CREATE)],
    ticketing: TicketingDep,
) -> DownloadLink:
    if not get_settings().demo_mode:
        raise NotFoundError("Not available outside demo mode.")
    start: int | None = None
    if body.type == ImportType.SERVICE_REQUESTS:
        highest = await session.scalar(select(func.max(ServiceRequest.number)))
        start = int(highest[2:]) + 1 if highest else None
    seed = random.SystemRandom().randint(1, 1_000_000)
    data = ticketing.generate_export(
        body.type, rows=body.rows, seed=seed, start_number=start, with_errors=body.with_errors
    )
    stored = await store_file(
        session,
        data,
        f"itsm-export-{body.type.value}.csv",
        "text/csv",
        "sample",
        ttl=service.SAMPLE_TTL,
        created_by=user,
    )
    link = signed_url(stored)
    await commit(session)
    return link


@router.get(
    "/{import_id}",
    response_model=ImportJobDetail,
    summary="Import detail",
    dependencies=[_READ],
    responses=problem_responses(401, 403, 404),
)
async def get_import(import_id: int, session: SessionDep, user: CurrentUser) -> ImportJobDetail:
    return await service.build_detail(session, await _get(session, import_id), user)


@router.get(
    "/{import_id}/errors",
    response_model=Page[ImportRowIssue],
    summary="Row errors and warnings of an import",
    dependencies=[_READ],
    responses=problem_responses(401, 403, 404, 422),
)
async def list_import_errors(
    import_id: int,
    session: SessionDep,
    paging: PageParamsDep,
    severity: Annotated[Literal["error", "warning"] | None, Query()] = None,
    code: Annotated[list[str] | None, Query()] = None,
) -> Page[ImportRowIssue]:
    await _get(session, import_id)
    stmt = (
        select(ImportJobError)
        .where(ImportJobError.import_job_id == import_id)
        .order_by(ImportJobError.row_number, ImportJobError.id)
    )
    if severity is not None:
        stmt = stmt.where(ImportJobError.severity == severity)
    codes = [c.strip() for value in code or [] for c in value.split(",") if c.strip()]
    if codes:
        stmt = stmt.where(ImportJobError.code.in_(codes))
    rows, meta = await fetch_page(session, stmt, paging)
    return Page[ImportRowIssue](data=[ImportRowIssue.model_validate(r) for r in rows], meta=meta)


@router.post(
    "/{import_id}/actions/cancel",
    response_model=ImportJobDetail,
    summary="Cancel a queued or running import",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def cancel_import(
    import_id: int,
    session: SessionDep,
    user: Annotated[User, Depends(_CREATE)],
    _body: EmptyBody | None = None,
) -> ImportJobDetail:
    job = await _get(session, import_id)
    await service.cancel_import(session, job)
    detail = await service.build_detail(session, job, user)
    await commit(session)
    return detail


@router.get(
    "/{import_id}/file",
    response_model=DownloadLink,
    summary="Download the original upload",
    dependencies=[_READ],
    responses=problem_responses(401, 403, 404),
)
async def download_original(import_id: int, session: SessionDep) -> DownloadLink:
    job = await _get(session, import_id)
    return signed_url(job.file)


@router.get(
    "/{import_id}/error-report",
    response_model=DownloadLink,
    summary="Download the failed-row report (original columns + issue columns)",
    dependencies=[_READ],
    responses=problem_responses(401, 403, 404),
)
async def download_error_report(import_id: int, session: SessionDep) -> DownloadLink:
    job = await _get(session, import_id)
    if job.error_report_file is None:
        raise NotFoundError("This import has no error report.")
    return signed_url(job.error_report_file)
