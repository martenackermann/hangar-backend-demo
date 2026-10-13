"""H1 write services and the pipeline summary. Services mutate, never commit."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    LR_TERMINAL,
    REGION_MUTABLE_STATUSES,
    SR_TERMINAL,
    DeliveryType,
    PipelineStage,
    ServiceRequestStatus,
    stage_of,
)
from app.core.errors import ConflictError, ValidationProblem
from app.core.models import (
    Contact,
    LeaseReturn,
    LeaseReturnComment,
    Region,
    ServiceRequest,
    StatusTransition,
    User,
)
from app.core.read_models import sr_needs_attention_clause
from app.core.region_resolver import apply_lease_campus, apply_region, set_region_override
from app.core.rules import normalize_cost_center, normalize_email, normalize_zip
from app.core.timeutil import business_datetime, business_today, now_utc, to_business_date
from app.core.transitions import check_version

from .queries import (
    PipelineFilters,
    business_date,
    pipeline_clauses,
    recent_closed_clause,
    stage_label,
    status_meta,
)
from .schemas import (
    AgingSplit,
    ContactPatch,
    LeaseReturnPatch,
    PipelineStageSummary,
    PipelineStatusCount,
    PipelineSummary,
    PipelineTotals,
    PipelineTrends,
    ServiceRequestPatch,
)

SR = ServiceRequestStatus
LOCKED_MESSAGE = "Region is locked after staging started."


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _zip(value: str | None, field: str) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = normalize_zip(value)
    if normalized is None:
        raise ValidationProblem({field: ["Enter a 4- or 5-digit zip code."]})
    return normalized


# --- service requests ------------------------------------------------------------------------------


async def patch_service_request(
    session: AsyncSession, sr: ServiceRequest, body: ServiceRequestPatch, actor: User
) -> None:
    """API.md §4.3. Region-relevant fields (zip, city, cost centre, region, delivery type)
    are locked outside ``REGION_MUTABLE_STATUSES`` (SPEC_RESOLUTIONS #29); street, room,
    building, country, phone and note stay editable until the request is closed."""
    check_version(sr, body.version)
    if sr.status in SR_TERMINAL:
        raise ConflictError("Closed requests are read-only.")
    fields = body.model_fields_set
    address = body.delivery_address
    address_fields = address.model_fields_set if address is not None else set()

    changes: dict[str, Any] = {}
    if address is not None:
        mapping = {
            "street": "delivery_street",
            "city": "delivery_city",
            "country": "delivery_country",
            "room": "delivery_room",
            "building": "delivery_building",
        }
        for key, column in mapping.items():
            if key in address_fields:
                changes[column] = _clean(getattr(address, key))
        if "zip" in address_fields:
            changes["delivery_zip"] = _zip(address.zip, "deliveryAddress.zip")
    if "cost_center" in fields:
        changes["cost_center"] = normalize_cost_center(body.cost_center)
    if "onsite_contact_phone" in fields:
        changes["onsite_contact_phone"] = _clean(body.onsite_contact_phone)
    if "note" in fields:
        changes["note"] = _clean(body.note)

    region_columns = {"delivery_zip", "delivery_city", "cost_center"}
    region_changed = any(
        column in changes and changes[column] != getattr(sr, column) for column in region_columns
    )
    region_override = False
    if "region_id" in fields:
        if body.region_id is None:
            region_override = sr.region_overridden  # clear the override + re-resolve
        else:
            region_override = not (sr.region_overridden and body.region_id == sr.region_id)
    delivery_changed = (
        "delivery_type_override" in fields
        and body.delivery_type_override != sr.delivery_type_override
    )
    if (region_changed or region_override or delivery_changed) and (
        sr.status not in REGION_MUTABLE_STATUSES
    ):
        raise ConflictError(LOCKED_MESSAGE)
    if (
        delivery_changed
        and sr.is_campus
        and body.delivery_type_override
        not in (
            None,
            DeliveryType.TECHNICIAN,
        )
    ):
        raise ValidationProblem(
            {"deliveryTypeOverride": ["Campus requests are always delivered by a technician."]}
        )

    region: Region | None = None
    if region_override and body.region_id is not None:
        region = await session.get(Region, body.region_id)
        if region is None or not region.is_active:
            raise ValidationProblem({"regionId": ["Unknown or inactive region."]})

    for column, value in changes.items():
        setattr(sr, column, value)
    if delivery_changed:
        sr.delivery_type_override = body.delivery_type_override
    if region_override:
        await set_region_override(session, sr, region)
    elif region_changed:
        await apply_region(session, sr)
    await session.flush()


# --- lease returns ---------------------------------------------------------------------------------


async def patch_lease(
    session: AsyncSession, lease: LeaseReturn, body: LeaseReturnPatch, actor: User
) -> None:
    check_version(lease, body.version)
    if lease.status in LR_TERMINAL:
        raise ConflictError("Closed lease returns are read-only.")
    if "escalation_email" in body.model_fields_set:
        lease.escalation_email = normalize_email(body.escalation_email)
    await session.flush()


async def add_comment(
    session: AsyncSession, lease: LeaseReturn, body: str, actor: User
) -> LeaseReturnComment:
    comment = LeaseReturnComment(lease_return_id=lease.id, body=body, author_user_id=actor.id)
    session.add(comment)
    await session.flush()
    await session.refresh(comment, ["author", "created_at"])
    return comment


# --- contacts ----------------------------------------------------------------------------------------


async def patch_contact(session: AsyncSession, contact: Contact, body: ContactPatch) -> None:
    """``PATCH /contacts/{id}``; address changes re-check the campus flag of open leases."""
    fields = body.model_fields_set
    if "gender" in fields:
        if body.gender is None:
            raise ValidationProblem({"gender": ["Choose a value."]})
        contact.gender = body.gender
    if "email" in fields:
        contact.email = normalize_email(body.email)
    for key in ("phone", "mobile", "department", "street", "city", "building", "room"):
        if key in fields:
            setattr(contact, key, _clean(getattr(body, key)))
    if "zip" in fields:
        contact.zip = _zip(body.zip, "zip")
    if fields & {"zip", "city"}:
        leases = (
            await session.scalars(
                select(LeaseReturn).where(
                    LeaseReturn.contact_id == contact.id, LeaseReturn.status.not_in(LR_TERMINAL)
                )
            )
        ).all()
        for lease in leases:
            lease.contact = contact
            await apply_lease_campus(session, lease)
    await session.flush()


# --- pipeline ---------------------------------------------------------------------------------------


async def _count(session: AsyncSession, *clauses: Any) -> int:
    stmt = select(func.count()).select_from(ServiceRequest).where(*clauses)
    return int((await session.execute(stmt)).scalar_one())


async def pipeline_summary(session: AsyncSession, filters: PipelineFilters) -> PipelineSummary:
    now = now_utc()
    today = business_today(now)
    scope = pipeline_clauses(filters)
    needs = sr_needs_attention_clause(today=today, now=now)
    rows = await session.execute(
        select(
            ServiceRequest.status,
            func.count(),
            func.min(ServiceRequest.status_changed_at),
            func.count().filter(needs),
        )
        .where(*scope, recent_closed_clause(today))
        .group_by(ServiceRequest.status)
    )
    by_status = {
        SR(status): (count, oldest, attention) for status, count, oldest, attention in rows
    }
    stages: list[PipelineStageSummary] = []
    for stage in PipelineStage:
        statuses: list[PipelineStatusCount] = []
        for status in SR:
            if stage_of(status) != stage:
                continue
            count, oldest, attention = by_status.get(status, (0, None, 0))
            status_label, tone = status_meta(status)
            statuses.append(
                PipelineStatusCount(
                    status=status,
                    label=status_label,
                    tone=tone,  # type: ignore[arg-type]
                    count=int(count),
                    oldest_status_changed_at=oldest,
                    attention_count=int(attention),
                )
            )
        stages.append(
            PipelineStageSummary(
                stage=stage,
                label=stage_label(stage),
                count=sum(s.count for s in statuses),
                statuses=statuses,
            )
        )
    open_ = ServiceRequest.status.not_in(SR_TERMINAL)
    week_ago: date = today - timedelta(days=6)
    totals = PipelineTotals(
        open=await _count(session, *scope, open_),
        vip_open=await _count(
            session, *pipeline_clauses(filters, vip=False), open_, ServiceRequest.is_vip.is_(True)
        ),
        needs_attention=await _count(session, *scope, open_, needs),
        completed_last_7_days=await _count(
            session,
            *scope,
            ServiceRequest.status == SR.COMPLETED,
            business_date(ServiceRequest.completed_at) >= week_ago,
        ),
        cancelled_last_7_days=await _count(
            session,
            *scope,
            ServiceRequest.status == SR.CANCELLED,
            business_date(ServiceRequest.cancelled_at) >= week_ago,
        ),
        appointments_today=await _count(
            session,
            *scope,
            ServiceRequest.status != SR.CANCELLED,
            business_date(ServiceRequest.appointment_start_at) == today,
        ),
    )
    await _board_extras(session, scope, stages, totals, today, now)
    trends = await _trends(session, scope, today)
    return PipelineSummary(generated_at=now, stages=stages, totals=totals, trends=trends)


#: DESIGN.md §3.5 stage SLAs in days (the frontend mirrors them as ``STAGE_SLA_DAYS``).
STAGE_SLA_DAYS: dict[PipelineStage, int | None] = {
    PipelineStage.IMPORTED: 2,
    PipelineStage.INVITED: 10,
    PipelineStage.BOOKED: 14,
    PipelineStage.PERSONALIZING: 3,
    PipelineStage.READY: 2,
    PipelineStage.DELIVERY: 3,
    PipelineStage.DEPLOYED: 5,
    PipelineStage.CLOSED: None,
}


def aging_level(stage: PipelineStage, since: datetime, now: datetime) -> str:
    sla = STAGE_SLA_DAYS[stage]
    if not sla:
        return "on_time"
    ratio = (now - since).total_seconds() / (sla * 86_400)
    return "on_time" if ratio <= 0.6 else "aging" if ratio <= 1 else "overdue"


async def _board_extras(
    session: AsyncSession,
    scope: list[Any],
    stages: list[PipelineStageSummary],
    totals: PipelineTotals,
    today: date,
    now: datetime,
) -> None:
    by_stage = {s.stage: s for s in stages}
    rows = await session.execute(
        select(ServiceRequest.status, ServiceRequest.status_changed_at).where(
            *scope, recent_closed_clause(today)
        )
    )
    splits: dict[PipelineStage, dict[str, int]] = {
        stage: {"on_time": 0, "aging": 0, "overdue": 0} for stage in PipelineStage
    }
    for status, since in rows.all():
        stage = stage_of(status)
        splits[stage][aging_level(stage, since, now)] += 1
    for stage, split in splits.items():
        by_stage[stage].aging = AgingSplit(**split)

    stage_case = {s.value: stage_of(s).value for s in SR}
    entered = await session.execute(
        select(StatusTransition.from_status, StatusTransition.to_status)
        .join(ServiceRequest, ServiceRequest.id == StatusTransition.entity_id)
        .where(
            StatusTransition.entity_type == "service_request",
            StatusTransition.occurred_at >= now - timedelta(hours=24),
            *scope,
        )
    )
    for from_status, to_status in entered.all():
        to_stage = stage_case.get(to_status)
        if to_stage is None or stage_case.get(from_status or "") == to_stage:
            continue
        by_stage[PipelineStage(to_stage)].entered_last_24h += 1

    durations = (
        await session.execute(
            select(ServiceRequest.imported_at, ServiceRequest.completed_at).where(
                *scope,
                ServiceRequest.status == SR.COMPLETED,
                ServiceRequest.completed_at >= now - timedelta(days=30),
            )
        )
    ).all()
    days = sorted(
        (completed - imported).total_seconds() / 86_400
        for imported, completed in durations
        if imported and completed
    )
    totals.lead_time_days = round(median(days), 1) if days else None


async def _trends(session: AsyncSession, scope: list[Any], today: date) -> PipelineTrends:
    days = [today - timedelta(days=offset) for offset in range(11, -1, -1)]
    rows = (
        await session.execute(
            select(
                ServiceRequest.imported_at,
                func.coalesce(ServiceRequest.completed_at, ServiceRequest.cancelled_at),
                ServiceRequest.completed_at,
            ).where(*scope)
        )
    ).all()
    open_: list[int] = []
    imported: list[int] = []
    completed: list[int] = []
    for day in days:
        end = business_datetime(day + timedelta(days=1), 0)
        open_.append(
            sum(1 for imp, closed, _ in rows if imp < end and (closed is None or closed >= end))
        )
        imported.append(sum(1 for imp, _, _ in rows if to_business_date(imp) == day))
        completed.append(
            sum(1 for _, _, done in rows if done is not None and to_business_date(done) == day)
        )
    return PipelineTrends(days=days, open=open_, imported=imported, completed=completed)
