"""Import jobs: upload, detection, read models, cancel, templates and samples (API.md §5.1)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import can
from app.core.db import after_commit
from app.core.enums import (
    ImportJobStatus,
    ImportType,
    JobKind,
    JobStatus,
    LocationType,
    SiteKind,
)
from app.core.errors import ApiError, TransitionNotAllowed, ValidationProblem
from app.core.files import Upload, store_file
from app.core.jobs import enqueue, finish_job
from app.core.models import StockLocation, User
from app.core.read_models.refs import location_ref, user_ref

from ..importing.formats import SPECS, detect_type
from ..importing.issues import FileProblem
from ..importing.processor import TERMINAL, job_counts
from ..importing.reader import ROW_COL, RawTable, read_table
from ..importing.validate import header_analysis
from ..models import ImportJob, ImportJobError
from ..schemas import (
    CodeMessage,
    ImportCounts,
    ImportDetection,
    ImportJobDetail,
    ImportJobSummary,
    ImportOptionsIn,
    ImportOptionsOut,
    ImportTemplate,
    PostProcess,
    TemplateColumn,
    TemplateOption,
    TemplatePackedField,
    TemplatePackedKey,
    TopErrorCode,
)

ACTIVE = frozenset({ImportJobStatus.QUEUED, ImportJobStatus.PROCESSING})


def file_problem_error(problem: FileProblem) -> ApiError:
    return ApiError(
        problem.message,
        status=problem.status,
        code=problem.code,
        title="File rejected",
        errors=problem.errors or {"file": [problem.message]},
    )


def parse_table(upload: Upload, max_rows: int) -> RawTable:
    try:
        return read_table(upload.data, upload.extension, max_rows=max_rows)
    except FileProblem as problem:
        raise file_problem_error(problem) from problem


# --- detection -----------------------------------------------------------------------------


def detect(upload: Upload, max_rows: int) -> ImportDetection:
    """``POST /imports/detect``: never raises for file-level problems (they go to ``problems``)."""
    try:
        table = read_table(upload.data, upload.extension, max_rows=max_rows)
    except FileProblem as problem:
        return ImportDetection(
            detected_type=None,
            delimiter=None,
            headers=[],
            normalized_headers=[],
            missing_columns=[],
            extra_columns=[],
            row_count=0,
            preview=[],
            problems=[CodeMessage(code=problem.code, message=problem.message)],
        )
    detected = detect_type(table.normalized)
    problems: list[CodeMessage] = []
    missing: list[str] = []
    extra: list[str] = []
    if detected is None:
        problems.append(
            CodeMessage(
                code="type_not_detected",
                message="The import type could not be detected from the column headers.",
            )
        )
    else:
        missing, extra = header_analysis(table.normalized, SPECS[detected])
    names = [n for n in table.frame.columns if n != ROW_COL]
    preview = [
        {name: record.get(name) for name in names}
        for record in table.frame.head(5).iter_rows(named=True)
    ]
    return ImportDetection(
        detected_type=detected,
        delimiter=table.delimiter,  # pyright: ignore[reportArgumentType]
        headers=table.headers,
        normalized_headers=table.normalized,
        missing_columns=missing,
        extra_columns=extra,
        row_count=table.row_count,
        preview=preview,
        problems=problems,
    )


# --- upload --------------------------------------------------------------------------------


def parse_options(raw: str | None) -> ImportOptionsIn:
    if raw is None or not raw.strip():
        return ImportOptionsIn()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationProblem({"options": ["Options must be a JSON object."]}) from exc
    if not isinstance(data, dict):
        raise ValidationProblem({"options": ["Options must be a JSON object."]})
    try:
        return ImportOptionsIn.model_validate(data)
    except ValidationError as exc:
        errors: dict[str, list[str]] = {}
        for error in exc.errors():
            path = ".".join(str(p) for p in ("options", *error["loc"]))
            message = "Unknown field." if error["type"] == "extra_forbidden" else error["msg"]
            errors.setdefault(path, []).append(message)
        raise ValidationProblem(errors) from exc


async def _target_location(session: AsyncSession, location_id: int | None) -> StockLocation:
    field = "options.targetLocationId"
    if location_id is None:
        raise ValidationProblem({field: ["Choose the hub location the devices are received into."]})
    location = await session.get(StockLocation, location_id)
    if (
        location is None
        or location.type not in (LocationType.GOODS_IN, LocationType.STAGING)
        or location.site is None
        or location.site.kind != SiteKind.HUB
        or not location.is_active
    ):
        raise ValidationProblem(
            {field: ["Choose an active hub location of type goods in or staging."]}
        )
    return location


async def create_import(
    session: AsyncSession,
    user: User,
    upload: Upload,
    *,
    forced_type: ImportType | None,
    options_raw: str | None,
    max_rows: int,
) -> ImportJob:
    table = parse_table(upload, max_rows)
    options = parse_options(options_raw)
    import_type = forced_type or detect_type(table.normalized)
    if import_type is None:
        raise ApiError(
            "The import type could not be detected from the column headers.",
            status=422,
            code="type_not_detected",
            title="Type not detected",
            errors={"file": ["The import type could not be detected from the column headers."]},
        )
    spec = SPECS[import_type]
    missing, extra = header_analysis(table.normalized, spec)
    if missing:
        message = f"Missing columns: {', '.join(missing)}"
        raise ApiError(
            message + ".",
            status=422,
            code="missing_columns",
            title="Missing columns",
            errors={"file": [message]},
        )
    stored_options: dict[str, Any] = {"dryRun": options.dry_run}
    if import_type == ImportType.ASSETS_NEW:
        location = await _target_location(session, options.target_location_id)
        stored_options["targetLocationId"] = location.id
    if import_type == ImportType.LEASE_RETURNS:
        stored_options["markMissingAsProbablyClosed"] = options.mark_missing_as_probably_closed

    import_job_id = int(
        (
            await session.execute(
                text("SELECT nextval(pg_get_serial_sequence('import_jobs', 'id'))")
            )
        ).scalar_one()
    )
    stored = await store_file(
        session,
        upload.data,
        upload.filename,
        upload.content_type,
        "import_upload",
        entity=("import_job", import_job_id),
        created_by=user,
    )
    job = await enqueue(
        session,
        "imports.process",
        kind=JobKind.IMPORT,
        created_by=user,
        entity=("import_job", import_job_id),
        import_job_id=import_job_id,
    )
    import_job = ImportJob(
        id=import_job_id,
        background_job_id=job.id,
        type=import_type,
        type_detected=forced_type is None,
        status=ImportJobStatus.QUEUED,
        file_id=stored.id,
        original_filename=upload.filename,
        options=stored_options,
        total_rows=table.row_count,
        job_warnings=(
            [{"code": "extra_columns", "message": f"Ignored columns: {', '.join(extra)}."}]
            if extra
            else []
        ),
        created_by=user.id,
    )
    session.add(import_job)
    await session.flush()
    return import_job


# --- cancel --------------------------------------------------------------------------------


async def cancel_import(session: AsyncSession, job: ImportJob) -> None:
    """Queued → cancelled at once; processing → cooperative (checked between chunks)."""
    if job.status in TERMINAL:
        raise TransitionNotAllowed(
            f"Import #{job.id} is already '{job.status}'.",
            action="cancel",
            current_status=str(job.status),
            allowed_actions=[],
        )
    if job.status == ImportJobStatus.QUEUED:
        job.status = ImportJobStatus.CANCELLED
        job.cancel_requested = True
        job.finished_at = func.now()
        background_job_id = job.background_job_id
        result = {
            "kind": "import",
            "importJobId": job.id,
            "status": ImportJobStatus.CANCELLED.value,
            "counts": job_counts(job),
        }

        async def _finish() -> None:
            await finish_job(background_job_id, JobStatus.CANCELLED, result, message="Cancelled")

        after_commit(session, _finish)
    else:
        job.cancel_requested = True
    await session.flush()
    await session.refresh(job)


# --- read models ---------------------------------------------------------------------------


def progress_percent(job: ImportJob) -> int | None:
    if job.status in (ImportJobStatus.COMPLETED, ImportJobStatus.COMPLETED_WITH_ERRORS):
        return 100
    if not job.total_rows:
        return None if job.status in ACTIVE else 0
    return min(100, round(job.processed_rows * 100 / job.total_rows))


def available_actions(job: ImportJob, user: User) -> list[str]:
    if job.status in ACTIVE and not job.cancel_requested and can(user, "imports.create"):
        return ["cancel"]
    return []


def build_summary(job: ImportJob, user: User) -> ImportJobSummary:
    creator = user_ref(job.creator)
    assert creator is not None
    return ImportJobSummary(
        id=job.id,
        job_id=job.background_job_id,
        type=job.type,
        type_detected=job.type_detected,
        status=job.status,
        original_filename=job.original_filename,
        dry_run=job.dry_run,
        counts=ImportCounts.model_validate(job_counts(job)),
        progress_percent=progress_percent(job),
        created_by=creator,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        available_actions=available_actions(job, user),
    )


async def build_detail(session: AsyncSession, job: ImportJob, user: User) -> ImportJobDetail:
    summary = build_summary(job, user)
    options = job.options or {}
    location = None
    if options.get("targetLocationId"):
        location = location_ref(await session.get(StockLocation, options["targetLocationId"]))
    top = (
        await session.execute(
            select(ImportJobError.code, ImportJobError.severity, func.count())
            .where(ImportJobError.import_job_id == job.id)
            .group_by(ImportJobError.code, ImportJobError.severity)
            .order_by(func.count().desc(), ImportJobError.code)
            .limit(12)
        )
    ).all()
    warnings = [CodeMessage.model_validate(w) for w in (job.job_warnings or [])]
    if job.failure:
        warnings.append(CodeMessage(code="import_failed", message=job.failure))
    return ImportJobDetail(
        **summary.model_dump(by_alias=False),
        options=ImportOptionsOut(
            dry_run=bool(options.get("dryRun")),
            target_location_id=options.get("targetLocationId"),
            mark_missing_as_probably_closed=bool(options.get("markMissingAsProbablyClosed")),
            target_location=location,
        ),
        post_process=PostProcess.model_validate(job.post_process or {}),
        job_level_warnings=warnings,
        has_error_report=job.error_report_file_id is not None,
        top_error_codes=[
            TopErrorCode.model_validate({"code": code, "severity": severity, "count": int(count)})
            for code, severity, count in top
        ],
    )


# --- templates -----------------------------------------------------------------------------


def templates() -> list[ImportTemplate]:
    result = []
    for import_type in ImportType:
        spec = SPECS[import_type]
        result.append(
            ImportTemplate(
                type=spec.type,
                label=spec.label,
                description=spec.description,
                signature_headers=list(spec.signature),
                upsert_key=spec.key,
                columns=[
                    TemplateColumn(
                        name=c.name,
                        required=c.required,
                        format=c.format,
                        description=c.description,
                        example=c.example,
                    )
                    for c in spec.columns
                ],
                packed_fields=[
                    TemplatePackedField(
                        column=column,
                        keys=[
                            TemplatePackedKey(
                                key=k.key,
                                required=k.required or k.required_unless_removal,
                                description=k.description,
                            )
                            for k in keys
                        ],
                    )
                    for column, keys in spec.packed.items()
                ],
                options=[
                    TemplateOption(name=o.name, required=o.required, description=o.description)
                    for o in spec.options
                ],
            )
        )
    return result


SAMPLE_TTL = timedelta(days=1)
