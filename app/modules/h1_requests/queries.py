"""List filters shared by the list, export and pipeline endpoints (API.md §4.1, §4.5, §4.7).

Dependencies parse the query string once (camelCase params, comma-separated lists — #39)
into a frozen filter object; ``*_clauses`` turn it into SQL predicates so the list, the
xlsx export ("follows the active list filter", SPEC §6.2) and the pipeline agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, Query
from sqlalchemy import ColumnElement, case, exists, false, func, not_, or_, select

from app.core.config import get_settings
from app.core.enums import (
    ENUM_META,
    LR_TERMINAL,
    SR_TERMINAL,
    AttentionFlag,
    DeliveryType,
    LeaseReturnStatus,
    PipelineStage,
    ProcurementType,
    ServiceRequestStatus,
    stage_of,
)
from app.core.errors import ValidationProblem
from app.core.models import Contact, LeaseReturn, Region, ServiceRequest, User
from app.core.pagination import csv_enum, csv_ints, escape_like
from app.core.read_models import (
    LEASE_FLAGS,
    SR_FLAGS,
    lease_attention_clause,
    lease_needs_attention_clause,
    sr_attention_clause,
    sr_needs_attention_clause,
    vip_clause,
)
from app.core.timeutil import business_today

SR = ServiceRequestStatus
LR = LeaseReturnStatus
type VipMode = Literal["exclude", "include", "only"]

VipParam = Annotated[
    VipMode,
    Query(description="exclude (default) hides every VIP record; include shows all; only VIPs."),
]


def business_date(column: Any) -> ColumnElement[Any]:
    """``timestamptz`` → calendar date in the business time zone."""
    return func.date(func.timezone(get_settings().business_tz, column))


def _date_range(column: Any, start: date | None, end: date | None) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = []
    if start is not None:
        clauses.append(column >= start)
    if end is not None:
        clauses.append(column <= end)
    return clauses


def _check_range(name: str, start: date | None, end: date | None) -> None:
    if start is not None and end is not None and start > end:
        raise ValidationProblem({f"{name}To": [f"Must not be before {name}From."]})


def _ilike(term: str) -> str:
    return f"%{escape_like(term.strip())}%"


def contact_match(term: str) -> ColumnElement[bool]:
    pattern = _ilike(term)
    full_name = func.coalesce(Contact.first_name, "") + " " + Contact.last_name
    return or_(
        full_name.ilike(pattern, escape="\\"),
        Contact.email.ilike(pattern, escape="\\"),
        Contact.user_id.ilike(pattern, escape="\\"),
    )


def is_paused_clause(model: Any, today: date) -> ColumnElement[bool]:
    return or_(model.communication_paused.is_(True), model.communication_resume_date > today)


# --- service requests ------------------------------------------------------------------------------

#: enum order (DESIGN.md pipeline order) for ``sort=status``
SR_STATUS_ORDER = case(
    {status.value: index for index, status in enumerate(SR)}, value=ServiceRequest.status
)
LR_STATUS_ORDER = case(
    {status.value: index for index, status in enumerate(LR)}, value=LeaseReturn.status
)

SR_SORT: dict[str, Any] = {
    "number": ServiceRequest.number,
    "status": SR_STATUS_ORDER,
    "importedAt": ServiceRequest.imported_at,
    "statusChangedAt": ServiceRequest.status_changed_at,
    "appointmentStartAt": ServiceRequest.appointment_start_at,
    "communicationDate": ServiceRequest.communication_date,
    "region": Region.name,
}
LEASE_SORT: dict[str, Any] = {
    "retirementDate": LeaseReturn.retirement_date,
    "assetTag": LeaseReturn.asset_tag,
    "status": LR_STATUS_ORDER,
    "statusChangedAt": LeaseReturn.status_changed_at,
    "communicationDate": LeaseReturn.communication_date,
}


def statuses_of_stages(stages: list[PipelineStage]) -> list[SR]:
    wanted = set(stages)
    return [s for s in SR if stage_of(s) in wanted]


@dataclass(frozen=True, slots=True)
class SrFilters:
    q: str | None = None
    status: tuple[SR, ...] | None = None
    stage: tuple[PipelineStage, ...] | None = None
    region_ids: tuple[int, ...] = ()
    region_none: bool = False
    site_ids: tuple[int, ...] | None = None
    delivery_type: tuple[DeliveryType, ...] | None = None
    procurement_type: tuple[ProcurementType, ...] | None = None
    vip: VipMode = "exclude"
    campus: bool | None = None
    editor: str | None = None  # 'me' | 'none' | user id
    contact_id: int | None = None
    appointment_from: date | None = None
    appointment_to: date | None = None
    has_appointment: bool | None = None
    communication_from: date | None = None
    communication_to: date | None = None
    imported_from: date | None = None
    imported_to: date | None = None
    closed_from: date | None = None
    closed_to: date | None = None
    paused: bool | None = None
    attention: tuple[AttentionFlag, ...] | None = None
    needs_attention: bool | None = None
    include_closed: bool = False
    ids: tuple[int, ...] | None = None

    @property
    def has_region_filter(self) -> bool:
        return bool(self.region_ids) or self.region_none


def _check_editor(raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    if raw in ("me", "none") or raw.isdigit():
        return raw
    raise ValidationProblem({"editorId": ["Use 'me', 'none' or a user id."]})


def sr_filter_params(
    q: Annotated[str | None, Query(description="SR/task number, contact name, email or user id.")] = None,
    status: Annotated[list[SR] | None, Depends(csv_enum("status", SR))] = None,
    stage: Annotated[list[PipelineStage] | None, Depends(csv_enum("stage", PipelineStage))] = None,
    region_id: Annotated[list[int | str] | None, Depends(csv_ints("regionId", allow=("none",)))] = None,
    site_id: Annotated[list[int | str] | None, Depends(csv_ints("siteId"))] = None,
    delivery_type: Annotated[list[DeliveryType] | None, Depends(csv_enum("deliveryType", DeliveryType))] = None,
    procurement_type: Annotated[list[ProcurementType] | None, Depends(csv_enum("procurementType", ProcurementType))] = None,
    vip: VipParam = "exclude",
    campus: Annotated[bool | None, Query()] = None,
    editor_id: Annotated[str | None, Query(alias="editorId", description="me | none | user id")] = None,
    contact_id: Annotated[int | None, Query(alias="contactId")] = None,
    appointment_from: Annotated[date | None, Query(alias="appointmentFrom")] = None,
    appointment_to: Annotated[date | None, Query(alias="appointmentTo")] = None,
    has_appointment: Annotated[bool | None, Query(alias="hasAppointment")] = None,
    communication_from: Annotated[date | None, Query(alias="communicationFrom")] = None,
    communication_to: Annotated[date | None, Query(alias="communicationTo")] = None,
    imported_from: Annotated[date | None, Query(alias="importedFrom")] = None,
    imported_to: Annotated[date | None, Query(alias="importedTo")] = None,
    closed_from: Annotated[date | None, Query(alias="closedFrom")] = None,
    closed_to: Annotated[date | None, Query(alias="closedTo")] = None,
    paused: Annotated[bool | None, Query()] = None,
    attention: Annotated[list[AttentionFlag] | None, Depends(csv_enum("attention", AttentionFlag))] = None,
    needs_attention: Annotated[bool | None, Query(alias="needsAttention")] = None,
    include_closed: Annotated[bool, Query(alias="includeClosed")] = False,
    ids: Annotated[list[int | str] | None, Depends(csv_ints("ids"))] = None,
) -> SrFilters:  # fmt: skip
    for name, start, end in (
        ("appointment", appointment_from, appointment_to),
        ("communication", communication_from, communication_to),
        ("imported", imported_from, imported_to),
        ("closed", closed_from, closed_to),
    ):
        _check_range(name, start, end)
    regions = list(region_id or [])
    return SrFilters(
        q=q.strip() if q and q.strip() else None,
        status=tuple(status) if status else None,
        stage=tuple(stage) if stage else None,
        region_ids=tuple(int(r) for r in regions if r != "none"),
        region_none="none" in regions,
        site_ids=tuple(int(s) for s in site_id) if site_id else None,
        delivery_type=tuple(delivery_type) if delivery_type else None,
        procurement_type=tuple(procurement_type) if procurement_type else None,
        vip=vip,
        campus=campus,
        editor=_check_editor(editor_id),
        contact_id=contact_id,
        appointment_from=appointment_from,
        appointment_to=appointment_to,
        has_appointment=has_appointment,
        communication_from=communication_from,
        communication_to=communication_to,
        imported_from=imported_from,
        imported_to=imported_to,
        closed_from=closed_from,
        closed_to=closed_to,
        paused=paused,
        attention=tuple(attention) if attention else None,
        needs_attention=needs_attention,
        include_closed=include_closed,
        ids=tuple(int(i) for i in ids) if ids else None,
    )


SrFiltersDep = Annotated[SrFilters, Depends(sr_filter_params)]


def sr_clauses(
    f: SrFilters, *, user: User | None = None, today: date | None = None
) -> list[ColumnElement[bool]]:
    today = today or business_today()
    m = ServiceRequest
    clauses: list[ColumnElement[bool]] = []
    if f.status:
        clauses.append(m.status.in_(f.status))
    elif not f.include_closed:
        clauses.append(m.status.not_in(SR_TERMINAL))
    if f.stage:
        clauses.append(m.status.in_(statuses_of_stages(list(f.stage))))
    if f.has_region_filter:
        options: list[ColumnElement[bool]] = []
        if f.region_ids:
            options.append(m.region_id.in_(f.region_ids))
        if f.region_none:
            options.append(m.region_id.is_(None))
        clauses.append(or_(*options))
    if f.site_ids:
        clauses.append(m.supplying_site_id.in_(f.site_ids))
    if f.delivery_type:
        clauses.append(
            func.coalesce(m.delivery_type_override, m.delivery_type).in_(f.delivery_type)
        )
    if f.procurement_type:
        clauses.append(m.procurement_type.in_(f.procurement_type))
    clauses.append(vip_clause(m.is_vip.expression, f.vip))
    if f.campus is not None:
        clauses.append(m.is_campus.is_(f.campus))
    if f.editor == "none":
        clauses.append(m.editor_user_id.is_(None))
    elif f.editor == "me":
        clauses.append(m.editor_user_id == (user.id if user else None))
    elif f.editor is not None:
        clauses.append(m.editor_user_id == int(f.editor))
    if f.contact_id is not None:
        clauses.append(m.contact_id == f.contact_id)
    clauses += _date_range(
        business_date(m.appointment_start_at), f.appointment_from, f.appointment_to
    )
    if f.has_appointment is not None:
        column = m.appointment_start_at
        clauses.append(column.is_not(None) if f.has_appointment else column.is_(None))
    clauses += _date_range(m.communication_date, f.communication_from, f.communication_to)
    clauses += _date_range(business_date(m.imported_at), f.imported_from, f.imported_to)
    closed = business_date(func.coalesce(m.completed_at, m.cancelled_at))
    clauses += _date_range(closed, f.closed_from, f.closed_to)
    if f.paused is not None:
        paused = is_paused_clause(m, today)
        clauses.append(paused if f.paused else not_(paused))
    if f.attention:
        flags = [flag for flag in f.attention if flag in SR_FLAGS]
        clauses.append(
            or_(*(sr_attention_clause(flag, today=today) for flag in flags)) if flags else false()
        )
    if f.needs_attention is not None:
        needs = sr_needs_attention_clause(today=today)
        clauses.append(needs if f.needs_attention else not_(needs))
    if f.ids:
        clauses.append(m.id.in_(f.ids))
    if f.q:
        pattern = _ilike(f.q)
        clauses.append(
            or_(
                m.number.ilike(pattern, escape="\\"),
                m.task_number.ilike(pattern, escape="\\"),
                exists().where(Contact.id == m.contact_id, contact_match(f.q)),
            )
        )
    return clauses


def sr_select(f: SrFilters, user: User | None = None) -> Any:
    return (
        select(ServiceRequest)
        .outerjoin(Region, Region.id == ServiceRequest.region_id)
        .where(*sr_clauses(f, user=user))
    )


# --- lease returns ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeaseFilters:
    q: str | None = None
    status: tuple[LR, ...] | None = None
    vip: VipMode = "exclude"
    campus: bool | None = None
    retirement_from: date | None = None
    retirement_to: date | None = None
    communication_from: date | None = None
    communication_to: date | None = None
    linked: bool | None = None
    paused: bool | None = None
    attention: tuple[AttentionFlag, ...] | None = None
    needs_attention: bool | None = None
    include_closed: bool = False
    ids: tuple[int, ...] | None = None


def lease_filter_params(
    q: Annotated[str | None, Query(description="Asset tag, serial, user id, name or email.")] = None,
    status: Annotated[list[LR] | None, Depends(csv_enum("status", LR))] = None,
    vip: VipParam = "exclude",
    campus: Annotated[bool | None, Query()] = None,
    retirement_from: Annotated[date | None, Query(alias="retirementFrom")] = None,
    retirement_to: Annotated[date | None, Query(alias="retirementTo")] = None,
    communication_from: Annotated[date | None, Query(alias="communicationFrom")] = None,
    communication_to: Annotated[date | None, Query(alias="communicationTo")] = None,
    linked: Annotated[bool | None, Query()] = None,
    paused: Annotated[bool | None, Query()] = None,
    attention: Annotated[list[AttentionFlag] | None, Depends(csv_enum("attention", AttentionFlag))] = None,
    needs_attention: Annotated[bool | None, Query(alias="needsAttention")] = None,
    include_closed: Annotated[bool, Query(alias="includeClosed")] = False,
    ids: Annotated[list[int | str] | None, Depends(csv_ints("ids"))] = None,
) -> LeaseFilters:  # fmt: skip
    _check_range("retirement", retirement_from, retirement_to)
    _check_range("communication", communication_from, communication_to)
    return LeaseFilters(
        q=q.strip() if q and q.strip() else None,
        status=tuple(status) if status else None,
        vip=vip,
        campus=campus,
        retirement_from=retirement_from,
        retirement_to=retirement_to,
        communication_from=communication_from,
        communication_to=communication_to,
        linked=linked,
        paused=paused,
        attention=tuple(attention) if attention else None,
        needs_attention=needs_attention,
        include_closed=include_closed,
        ids=tuple(int(i) for i in ids) if ids else None,
    )


LeaseFiltersDep = Annotated[LeaseFilters, Depends(lease_filter_params)]

#: hidden by default (SPEC §4.2 "Default list hides replacement_ordered and closed")
LEASE_DEFAULT_HIDDEN = (LR.REPLACEMENT_ORDERED, *LR_TERMINAL)


def lease_clauses(f: LeaseFilters, *, today: date | None = None) -> list[ColumnElement[bool]]:
    today = today or business_today()
    m = LeaseReturn
    clauses: list[ColumnElement[bool]] = []
    if f.status:
        clauses.append(m.status.in_(f.status))
    elif not f.include_closed:
        clauses.append(m.status.not_in(LEASE_DEFAULT_HIDDEN))
    clauses.append(vip_clause(m.is_vip.expression, f.vip))
    if f.campus is not None:
        clauses.append(m.is_campus.is_(f.campus))
    clauses += _date_range(m.retirement_date, f.retirement_from, f.retirement_to)
    clauses += _date_range(m.communication_date, f.communication_from, f.communication_to)
    if f.linked is not None:
        clauses.append(
            m.service_request_id.is_not(None) if f.linked else m.service_request_id.is_(None)
        )
    if f.paused is not None:
        paused = is_paused_clause(m, today)
        clauses.append(paused if f.paused else not_(paused))
    if f.attention:
        flags = [flag for flag in f.attention if flag in LEASE_FLAGS]
        clauses.append(
            or_(*(lease_attention_clause(flag, today=today) for flag in flags))
            if flags
            else false()
        )
    if f.needs_attention is not None:
        needs = lease_needs_attention_clause(today=today)
        clauses.append(needs if f.needs_attention else not_(needs))
    if f.ids:
        clauses.append(m.id.in_(f.ids))
    if f.q:
        pattern = _ilike(f.q)
        clauses.append(
            or_(
                m.asset_tag.ilike(pattern, escape="\\"),
                m.serial_number.ilike(pattern, escape="\\"),
                m.user_id.ilike(pattern, escape="\\"),
                exists().where(Contact.id == m.contact_id, contact_match(f.q)),
            )
        )
    return clauses


def lease_select(f: LeaseFilters) -> Any:
    return select(LeaseReturn).where(*lease_clauses(f))


# --- pipeline ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipelineFilters:
    site_ids: tuple[int, ...] | None = None
    region_ids: tuple[int, ...] = ()
    region_none: bool = False
    delivery_type: tuple[DeliveryType, ...] | None = None
    vip: VipMode = "exclude"
    campus: bool | None = None


def pipeline_filter_params(
    site_id: Annotated[list[int | str] | None, Depends(csv_ints("siteId"))] = None,
    region_id: Annotated[list[int | str] | None, Depends(csv_ints("regionId", allow=("none",)))] = None,
    delivery_type: Annotated[list[DeliveryType] | None, Depends(csv_enum("deliveryType", DeliveryType))] = None,
    vip: VipParam = "exclude",
    campus: Annotated[bool | None, Query()] = None,
) -> PipelineFilters:  # fmt: skip
    regions = list(region_id or [])
    return PipelineFilters(
        site_ids=tuple(int(s) for s in site_id) if site_id else None,
        region_ids=tuple(int(r) for r in regions if r != "none"),
        region_none="none" in regions,
        delivery_type=tuple(delivery_type) if delivery_type else None,
        vip=vip,
        campus=campus,
    )


PipelineFiltersDep = Annotated[PipelineFilters, Depends(pipeline_filter_params)]


def pipeline_clauses(f: PipelineFilters, *, vip: bool = True) -> list[ColumnElement[bool]]:
    """Scope of the board (``vip=False`` ignores the VIP mode, for ``totals.vipOpen``)."""
    sr_filters = SrFilters(
        region_ids=f.region_ids,
        region_none=f.region_none,
        site_ids=f.site_ids,
        delivery_type=f.delivery_type,
        vip=f.vip if vip else "include",
        campus=f.campus,
        include_closed=True,
    )
    return sr_clauses(sr_filters)


def status_meta(status: SR) -> tuple[str, str | None]:
    meta = ENUM_META[SR][status.value]
    return meta.label, meta.tone.value if meta.tone else None


def stage_label(stage: PipelineStage) -> str:
    return ENUM_META[PipelineStage][stage.value].label


def recent_closed_clause(today: date, days: int = 14) -> ColumnElement[bool]:
    """Deployed / Closed columns count only the last ``days`` days (API.md §4.7)."""
    since = today - timedelta(days=days)
    m = ServiceRequest
    closed_on = business_date(func.coalesce(m.completed_at, m.cancelled_at, m.status_changed_at))
    return or_(m.status.not_in(SR_TERMINAL), closed_on >= since)


__all__ = [
    "LEASE_SORT",
    "SR_SORT",
    "LeaseFilters",
    "LeaseFiltersDep",
    "PipelineFilters",
    "PipelineFiltersDep",
    "SrFilters",
    "SrFiltersDep",
]
