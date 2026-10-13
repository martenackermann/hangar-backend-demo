"""Service request response models + builders (API.md §4.1/§4.2; SPEC_RESOLUTIONS #1).

Slices call :func:`build_sr_list_items` / :func:`build_sr_detail`; they never re-implement
these shapes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Literal

from sqlalchemy import ColumnElement, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gateways
from app.core.actions import actions
from app.core.enums import (
    AttentionFlag,
    DeliveryType,
    HoldSource,
    ItemSlot,
    LeaseReturnStatus,
    OsReinstallTiming,
    PipelineStage,
    ProcurementType,
    RegionRuleMatchKind,
    ServiceRequestStatus,
    stage_of,
)
from app.core.models import LeaseReturn, ServiceRequest, ServiceRequestItem, User
from app.core.read_models.attention import sr_attention
from app.core.read_models.refs import (
    article_ref,
    asset_summary,
    contact_ref,
    deviation_code_ref,
    region_ref,
    site_ref,
    user_ref,
)
from app.core.schemas import (
    ApiModel,
    ArticleRef,
    AssetSummary,
    BlockedAction,
    ContactRef,
    DeliveryAddress,
    DeviationCodeRef,
    RegionRef,
    SiteRef,
    UserRef,
)

type VipMode = Literal["exclude", "include", "only"]


class ServiceRequestItemOut(ApiModel):
    id: int
    slot: ItemSlot
    direction: Literal["deliver", "collect"]
    requested_type: str | None
    article: ArticleRef | None
    expected_asset_tag: str | None
    asset: AssetSummary | None
    assigned_at: datetime | None
    assigned_by: UserRef | None
    packed_at: datetime | None
    packed_by: UserRef | None
    verified_at: datetime | None
    verified_by: UserRef | None
    verified_serial: str | None


class ServiceRequestListItem(ApiModel):
    id: int
    number: str
    task_number: str | None
    status: ServiceRequestStatus
    stage: PipelineStage
    status_changed_at: datetime
    contact: ContactRef
    region: RegionRef | None
    supplying_site: SiteRef | None
    delivery_type: DeliveryType | None
    is_campus: bool
    is_vip: bool
    procurement_type: ProcurementType | None
    new_item_types: list[str]
    appointment_start_at: datetime | None
    appointment_end_at: datetime | None
    appointment_calendar_id: int | None
    appointment_calendar_name: str | None
    communication_date: date | None
    communication_paused: bool
    communication_resume_date: date | None
    editor: UserRef | None
    imported_at: datetime
    attention: list[AttentionFlag]
    available_actions: list[str]


class RegionRuleRef(ApiModel):
    id: int
    match_kind: RegionRuleMatchKind
    pattern: str


class DeviationOut(ApiModel):
    code: DeviationCodeRef
    comment: str | None


class LinkedLeaseReturn(ApiModel):
    id: int
    asset_tag: str
    status: LeaseReturnStatus


class Milestones(ApiModel):
    personalized_at: datetime | None
    order_closed_at: datetime | None
    provided_at: datetime | None
    shipped_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None


class ServiceRequestDetail(ServiceRequestListItem):
    version: int
    itsm_state: str | None
    assignment_group: str | None
    opened_at: datetime | None
    catalog_item: str | None
    short_description: str | None
    cost_center: str | None
    scope_of_delivery: str | None
    client_component: str | None
    requested_software: list[str]
    added_software: list[str]
    installed_software: list[str]
    note: str | None
    onsite_contact_phone: str | None
    delivery_address: DeliveryAddress
    region_rule: RegionRuleRef | None
    region_overridden: bool
    resolved_delivery_type: DeliveryType | None
    delivery_type_override: DeliveryType | None
    resume_status: ServiceRequestStatus | None
    communication_pause_reason: str | None
    hardware_unavailable_notified_at: datetime | None
    hold_source: HoldSource | None
    claimed_at: datetime | None
    without_asset_prep: bool
    os_reinstall: OsReinstallTiming | None
    deviation: DeviationOut | None
    items: list[ServiceRequestItemOut]
    linked_lease_returns: list[LinkedLeaseReturn]
    milestones: Milestones
    booking_link: str | None
    protocol_available: bool
    last_imported_at: datetime
    last_import_job_id: int | None
    blocked_actions: list[BlockedAction]


# --- builders -----------------------------------------------------------------------------


def build_item(item: ServiceRequestItem) -> ServiceRequestItemOut:
    slot = ItemSlot(item.slot)
    return ServiceRequestItemOut(
        id=item.id,
        slot=slot,
        direction="deliver" if slot.is_new else "collect",
        requested_type=item.requested_type,
        article=article_ref(item.article),
        expected_asset_tag=item.expected_asset_tag,
        asset=asset_summary(item.asset),
        assigned_at=item.assigned_at,
        assigned_by=user_ref(item.assigned_by_user),
        packed_at=item.packed_at,
        packed_by=user_ref(item.packed_by_user),
        verified_at=item.verified_at,
        verified_by=user_ref(item.verified_by_user),
        verified_serial=item.verified_serial,
    )


def _list_fields(sr: ServiceRequest, available: list[str]) -> dict[str, object]:
    contact = contact_ref(sr.contact)
    assert contact is not None
    return {
        "id": sr.id,
        "number": sr.number,
        "task_number": sr.task_number,
        "status": sr.status,
        "stage": stage_of(sr.status),
        "status_changed_at": sr.status_changed_at,
        "contact": contact,
        "region": region_ref(sr.region),
        "supplying_site": site_ref(sr.supplying_site),
        "delivery_type": sr.effective_delivery_type,
        "is_campus": sr.is_campus,
        "is_vip": sr.is_vip,
        "procurement_type": sr.procurement_type,
        "new_item_types": [i.requested_type for i in sr.new_items if i.requested_type],
        "appointment_start_at": sr.appointment_start_at,
        "appointment_end_at": sr.appointment_end_at,
        "appointment_calendar_id": sr.appointment_calendar_id,
        "appointment_calendar_name": sr.appointment_calendar_name,
        "communication_date": sr.communication_date,
        "communication_paused": sr.communication_paused,
        "communication_resume_date": sr.communication_resume_date,
        "editor": user_ref(sr.editor),
        "imported_at": sr.imported_at,
        "attention": sr_attention(sr),
        "available_actions": available,
    }


async def build_sr_list_item(
    session: AsyncSession, sr: ServiceRequest, user: User | None
) -> ServiceRequestListItem:
    available = await actions.available_names(session, sr, user)
    return ServiceRequestListItem(**_list_fields(sr, available))  # type: ignore[arg-type]


async def build_sr_list_items(
    session: AsyncSession, srs: Sequence[ServiceRequest], user: User | None
) -> list[ServiceRequestListItem]:
    return [await build_sr_list_item(session, sr, user) for sr in srs]


def booking_link(sr: ServiceRequest) -> str | None:
    region = sr.region
    if (
        sr.is_campus
        or region is None
        or not region.booking_destination_id
        or not region.booking_service_id
    ):
        return None
    return gateways.booking().build_booking_link(
        destination_id=region.booking_destination_id,
        service_id=region.booking_service_id,
        reference=sr.number,
    )


async def build_sr_detail(
    session: AsyncSession, sr: ServiceRequest, user: User | None
) -> ServiceRequestDetail:
    available, blocked = await actions.available(session, sr, user)
    leases = (
        await session.scalars(
            select(LeaseReturn)
            .where(LeaseReturn.service_request_id == sr.id)
            .order_by(LeaseReturn.asset_tag)
        )
    ).all()
    rule = sr.region_rule
    code = deviation_code_ref(sr.deviation_code)
    return ServiceRequestDetail(
        **_list_fields(sr, available),  # type: ignore[arg-type]
        version=sr.version,
        itsm_state=sr.itsm_state,
        assignment_group=sr.assignment_group,
        opened_at=sr.opened_at,
        catalog_item=sr.catalog_item,
        short_description=sr.short_description,
        cost_center=sr.cost_center,
        scope_of_delivery=sr.scope_of_delivery,
        client_component=sr.client_component,
        requested_software=list(sr.requested_software or []),
        added_software=list(sr.added_software or []),
        installed_software=list(sr.installed_software or []),
        note=sr.note,
        onsite_contact_phone=sr.onsite_contact_phone,
        delivery_address=DeliveryAddress(
            street=sr.delivery_street,
            zip=sr.delivery_zip,
            city=sr.delivery_city,
            country=sr.delivery_country,
            room=sr.delivery_room,
            building=sr.delivery_building,
        ),
        region_rule=RegionRuleRef(id=rule.id, match_kind=rule.match_kind, pattern=rule.pattern)
        if rule
        else None,
        region_overridden=sr.region_overridden,
        resolved_delivery_type=sr.delivery_type,
        delivery_type_override=sr.delivery_type_override,
        resume_status=sr.resume_status,
        communication_pause_reason=sr.communication_pause_reason,
        hardware_unavailable_notified_at=sr.hardware_unavailable_notified_at,
        hold_source=sr.hold_source,
        claimed_at=sr.claimed_at,
        without_asset_prep=sr.without_asset_prep,
        os_reinstall=sr.os_reinstall,
        deviation=DeviationOut(code=code, comment=sr.deviation_comment) if code else None,
        items=[build_item(i) for i in sr.items],
        linked_lease_returns=[
            LinkedLeaseReturn(id=item_.id, asset_tag=item_.asset_tag, status=item_.status)
            for item_ in leases
        ],
        milestones=Milestones(
            personalized_at=sr.personalized_at,
            order_closed_at=sr.order_closed_at,
            provided_at=sr.provided_at,
            shipped_at=sr.shipped_at,
            completed_at=sr.completed_at,
            cancelled_at=sr.cancelled_at,
        ),
        booking_link=booking_link(sr),
        protocol_available=sr.protocol_file_id is not None,
        last_imported_at=sr.last_imported_at,
        last_import_job_id=sr.last_import_job_id,
        blocked_actions=blocked,
    )


def vip_clause(column: ColumnElement[bool], mode: VipMode | None) -> ColumnElement[bool]:
    """``vip`` list filter (SPEC_RESOLUTIONS #11): default ``exclude`` hides every ``is_vip``."""
    if mode in (None, "exclude"):
        return column.is_(False)
    if mode == "only":
        return column.is_(True)
    return true()
