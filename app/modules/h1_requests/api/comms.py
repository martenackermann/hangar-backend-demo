"""Communications (API.md §4.8): mail log, dispatch settings, dispatch runs, templates."""

from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from jinja2 import TemplateError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.enums import JobKind, JobStatus, MailStatus, Trigger
from app.core.errors import ConflictError, NotFoundError, ValidationProblem, problem_responses
from app.core.files import signed_url
from app.core.jobs import enqueue
from app.core.models import StoredFile, User
from app.core.notifications import render_template, templates
from app.core.pagination import (
    OrderBy,
    Page,
    PageParamsDep,
    csv_enum,
    escape_like,
    fetch_page,
    sort_param,
    split_csv,
)
from app.core.read_models import user_ref
from app.core.schemas import EntityRef, JobAccepted
from app.core.timeutil import now_utc

from ..chain import ensure_settings, load_settings, next_scheduled_run
from ..dispatch import TASK_NAME, dry_run, run_in_progress
from ..models import DispatchRun, DispatchSettings, MailLog
from ..queries import business_date
from ..schemas import (
    DispatchRunCreate,
    DispatchRunOut,
    DispatchSettingsInput,
    DispatchSettingsOut,
    MailAttachmentOut,
    MailLogDetail,
    MailLogEntry,
    NotificationTemplateInfo,
    NotificationTemplateList,
    TemplatePreview,
)

router = APIRouter(tags=["communications"])

Reader = Annotated[User, Depends(require("comms.read"))]
MAIL_SORT = sort_param(
    {"queuedAt": MailLog.queued_at, "sentAt": MailLog.sent_at},
    default="-queuedAt",
    tiebreaker=MailLog.id,
)
ENTITY_TYPES = {
    "service_request",
    "lease_return",
    "asset",
    "calloff_order",
    "inventory",
    "import_job",
    "tour",
    "contact",
}


# --- mail log ----------------------------------------------------------------------------------------


def template_name(template_id: str) -> str:
    return templates.get(template_id).display_name if template_id in templates else template_id


def mail_entry(row: MailLog) -> dict[str, Any]:
    entity = None
    if row.entity_type in ENTITY_TYPES and row.entity_id is not None:
        entity = EntityRef(
            type=row.entity_type,  # type: ignore[arg-type]
            id=row.entity_id,
            label=row.entity_label or f"#{row.entity_id}",
        )
    return {
        "id": row.id,
        "notification_id": row.notification_id,
        "template_id": row.template_id,
        "template_name": template_name(row.template_id),
        "status": row.status,
        "to": list(row.to_addresses),
        "cc": list(row.cc_addresses),
        "subject": row.subject,
        "entity": entity,
        "trigger": row.trigger,
        "triggered_by": user_ref(row.triggerer),
        "queued_at": row.queued_at,
        "sent_at": row.sent_at,
        "error": row.error,
        "attachment_count": len(row.attachment_file_ids or []),
    }


@router.get(
    "/mail-log",
    response_model=Page[MailLogEntry],
    summary="Mail log",
    responses=problem_responses(401, 403, 422),
)
async def list_mail_log(
    session: SessionDep,
    _user: Reader,
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(MAIL_SORT)],
    status_: Annotated[list[MailStatus] | None, Depends(csv_enum("status", MailStatus))] = None,
    trigger: Annotated[list[Trigger] | None, Depends(csv_enum("trigger", Trigger))] = None,
    q: Annotated[str | None, Query(description="Recipient, subject or entity label.")] = None,
    template_id: Annotated[list[str] | None, Query(alias="templateId")] = None,
    entity_type: Annotated[str | None, Query(alias="entityType")] = None,
    entity_id: Annotated[int | None, Query(alias="entityId")] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> Page[MailLogEntry]:
    stmt = select(MailLog)
    if q and q.strip():
        pattern = f"%{escape_like(q.strip())}%"
        stmt = stmt.where(
            func.array_to_string(MailLog.to_addresses, " ").ilike(pattern, escape="\\")
            | MailLog.subject.ilike(pattern, escape="\\")
            | MailLog.entity_label.ilike(pattern, escape="\\")
        )
    templates_wanted = split_csv(template_id)
    if templates_wanted:
        stmt = stmt.where(MailLog.template_id.in_(templates_wanted))
    if status_:
        stmt = stmt.where(MailLog.status.in_(status_))
    if trigger:
        stmt = stmt.where(MailLog.trigger.in_(trigger))
    if entity_type:
        stmt = stmt.where(MailLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(MailLog.entity_id == entity_id)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValidationProblem({"to": ["Must not be before 'from'."]})
    if date_from is not None:
        stmt = stmt.where(business_date(MailLog.queued_at) >= date_from)
    if date_to is not None:
        stmt = stmt.where(business_date(MailLog.queued_at) <= date_to)
    rows, meta = await fetch_page(session, stmt.order_by(*order), paging)
    return Page[MailLogEntry](data=[MailLogEntry(**mail_entry(r)) for r in rows], meta=meta)


@router.get(
    "/mail-log/{mail_id}",
    response_model=MailLogDetail,
    summary="Mail log entry with rendered body",
    responses=problem_responses(401, 403, 404),
)
async def get_mail_log(mail_id: int, session: SessionDep, _user: Reader) -> MailLogDetail:
    row = await session.get(MailLog, mail_id)
    if row is None:
        raise NotFoundError(f"Mail {mail_id} not found.")
    attachments: list[MailAttachmentOut] = []
    if row.attachment_file_ids:
        files = (
            await session.scalars(
                select(StoredFile).where(StoredFile.id.in_(row.attachment_file_ids))
            )
        ).all()
        attachments = [
            MailAttachmentOut(
                file_id=f.id,
                filename=f.filename,
                content_type=f.content_type,
                size_bytes=f.size_bytes,
                download=signed_url(f),
            )
            for f in files
        ]
    return MailLogDetail(
        **mail_entry(row), body_html=row.body_html, body_text=row.body_text, attachments=attachments
    )


# --- dispatch settings ------------------------------------------------------------------------------


def settings_out(row: DispatchSettings) -> DispatchSettingsOut:
    values = {name: getattr(row, name) for name in DispatchSettingsInput.model_fields}
    return DispatchSettingsOut(
        **values,
        updated_at=row.updated_at or now_utc(),
        updated_by=user_ref(row.updater) if row.updated_by else None,
        next_scheduled_run_at=next_scheduled_run(),
    )


@router.get(
    "/dispatch-settings",
    response_model=DispatchSettingsOut,
    summary="Dispatch settings",
    responses=problem_responses(401, 403),
)
async def get_dispatch_settings(session: SessionDep, _user: Reader) -> DispatchSettingsOut:
    return settings_out(await load_settings(session))


@router.put(
    "/dispatch-settings",
    response_model=DispatchSettingsOut,
    summary="Update dispatch settings",
    responses=problem_responses(401, 403, 422),
)
async def put_dispatch_settings(
    body: DispatchSettingsInput,
    session: SessionDep,
    user: Annotated[User, Depends(require("comms.settings"))],
) -> DispatchSettingsOut:
    row = await ensure_settings(session)
    for name, value in body.model_dump(by_alias=False).items():
        setattr(row, name, value)
    row.updated_by = user.id
    row.updated_at = now_utc()
    await commit(session)
    await session.refresh(row, ["updater", "updated_at"])
    return settings_out(row)


# --- dispatch runs -----------------------------------------------------------------------------------


def run_out(run: DispatchRun) -> DispatchRunOut:
    return DispatchRunOut(
        id=run.id,
        trigger=run.trigger,
        dry_run=run.dry_run,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        counts=dict(run.counts or {}),
        skipped=dict(run.skipped or {}),
        errors=list(run.errors or []),
        planned=list(run.planned) if run.planned is not None else None,
        triggered_by=user_ref(run.triggerer),
        job_id=run.background_job_id,
    )


@router.get(
    "/comms/dispatch-runs",
    response_model=Page[DispatchRunOut],
    summary="Dispatch runs (newest first)",
    responses=problem_responses(401, 403, 422),
)
async def list_dispatch_runs(
    session: SessionDep,
    _user: Reader,
    paging: PageParamsDep,
    dry_run_: Annotated[
        bool | None, Query(alias="dryRun", description="Filter; omit for all runs.")
    ] = None,
) -> Page[DispatchRunOut]:
    stmt = select(DispatchRun)
    if dry_run_ is not None:
        stmt = stmt.where(DispatchRun.dry_run.is_(dry_run_))
    stmt = stmt.order_by(DispatchRun.started_at.desc(), DispatchRun.id.desc())
    rows, meta = await fetch_page(session, stmt, paging)
    return Page[DispatchRunOut](data=[run_out(r) for r in rows], meta=meta)


@router.get(
    "/comms/dispatch-runs/{run_id}",
    response_model=DispatchRunOut,
    summary="Dispatch run",
    responses=problem_responses(401, 403, 404),
)
async def get_dispatch_run(run_id: int, session: SessionDep, _user: Reader) -> DispatchRunOut:
    run = await session.get(DispatchRun, run_id)
    if run is None:
        raise NotFoundError(f"Dispatch run {run_id} not found.")
    return run_out(run)


@router.post(
    "/comms/dispatch-runs",
    response_model=DispatchRunOut | JobAccepted,
    summary="Run the dispatch now (202) or as a dry run (200)",
    responses={
        202: {"model": JobAccepted, "description": "Run queued"},
        **problem_responses(401, 403, 409, 422),
    },
)
async def create_dispatch_run(
    body: DispatchRunCreate,
    response: Response,
    session: SessionDep,
    user: Annotated[User, Depends(require("comms.dispatch"))],
) -> DispatchRunOut | JobAccepted:
    if body.dry_run:
        run = await dry_run(session, user)
        await commit(session)
        await session.refresh(run, ["triggerer"])
        return run_out(run)
    return await queue_run(session, user, response)


async def queue_run(session: AsyncSession, user: User, response: Response) -> JobAccepted:
    if await run_in_progress(session) is not None:
        raise ConflictError("A dispatch run is already in progress.")
    run = DispatchRun(
        trigger=Trigger.USER,
        dry_run=False,
        status=JobStatus.QUEUED,
        started_at=now_utc(),
        triggered_by=user.id,
        counts={},
        skipped={},
        errors=[],
    )
    session.add(run)
    await session.flush()
    job = await enqueue(
        session,
        TASK_NAME,
        kind=JobKind.COMMS_DISPATCH,
        created_by=user,
        dispatch_run_id=run.id,
    )
    run.background_job_id = job.id
    await commit(session)
    response.status_code = status.HTTP_202_ACCEPTED
    return JobAccepted(job_id=job.id, dispatch_run_id=run.id)


# --- notification templates -------------------------------------------------------------------------


@router.get(
    "/notification-templates",
    response_model=NotificationTemplateList,
    summary="Notification templates",
    responses=problem_responses(401, 403),
)
async def list_templates(session: SessionDep, _user: Reader) -> NotificationTemplateList:
    since = now_utc() - timedelta(days=7)
    counts = dict(
        (
            await session.execute(
                select(MailLog.template_id, func.count())
                .where(MailLog.status == MailStatus.SENT, MailLog.sent_at >= since)
                .group_by(MailLog.template_id)
            )
        ).all()
    )
    return NotificationTemplateList(
        data=[
            NotificationTemplateInfo(
                id=t.id,
                name=t.display_name,
                owner=t.owner,
                description=t.description,
                trigger=t.trigger,
                recipients=t.recipients,
                subject_template=t.subject,
                sent_last_7_days=int(counts.get(t.id, 0)),
                entity_type=t.entity_type if t.build_context is not None else None,
            )
            for t in templates.all()
        ]
    )


@router.get(
    "/notification-templates/{template_id}/preview",
    response_model=TemplatePreview,
    summary="Render a template (never sends)",
    responses=problem_responses(401, 403, 404, 422),
)
async def preview_template(
    template_id: str,
    session: SessionDep,
    _user: Reader,
    entity_type: Annotated[str | None, Query(alias="entityType")] = None,
    entity_id: Annotated[int | None, Query(alias="entityId")] = None,
) -> TemplatePreview:
    if template_id not in templates:
        raise NotFoundError(f"Unknown template '{template_id}'.")
    template = templates.get(template_id)
    if entity_id is not None:
        if template.build_context is None:
            raise ValidationProblem({"entityId": ["This template has no entity preview."]})
        if entity_type is not None and template.entity_type not in (None, entity_type):
            raise ValidationProblem(
                {"entityType": [f"This template renders {template.entity_type} records."]}
            )
        context = await template.build_context(session, entity_id)
    else:
        context = dict(template.sample_context)
    try:
        rendered = render_template(template, context)
    except TemplateError as exc:
        raise ValidationProblem(
            {"templateId": [f"The template could not be rendered: {exc}"]}
        ) from exc
    return TemplatePreview(subject=rendered.subject, html=rendered.html, text=rendered.text)
