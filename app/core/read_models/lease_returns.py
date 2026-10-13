"""Lease return response models + builders (API.md §4.5)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actions import actions
from app.core.enums import AttentionFlag, LeaseReturnStatus, ServiceRequestStatus
from app.core.models import Asset, LeaseReturn, LeaseReturnComment, User
from app.core.read_models.attention import lease_attention
from app.core.read_models.refs import asset_summary, contact_ref
from app.core.schemas import ApiModel, AssetSummary, BlockedAction, ContactRef
from app.core.timeutil import business_today


class LeaseSrRef(ApiModel):
    id: int
    number: str
    status: ServiceRequestStatus


class LeaseReturnListItem(ApiModel):
    id: int
    asset_tag: str
    serial_number: str | None
    model: str | None
    model_category: str | None
    retirement_date: date
    days_until_retirement: int
    contact: ContactRef | None
    user_id: str
    status: LeaseReturnStatus
    status_changed_at: datetime
    is_vip: bool
    is_campus: bool
    communication_date: date | None
    communication_paused: bool
    communication_resume_date: date | None
    service_request: LeaseSrRef | None
    comment_count: int
    attention: list[AttentionFlag]
    available_actions: list[str]


class LeaseReturnDetail(LeaseReturnListItem):
    version: int
    checked_in_at: datetime | None
    install_status: str | None
    ci_name: str | None
    cost_center: str | None
    communication_pause_reason: str | None
    info_sent_at: datetime | None
    reminder_1_sent_at: datetime | None
    reminder_2_sent_at: datetime | None
    escalation_sent_at: datetime | None
    escalation_email: str | None
    linked_at: datetime | None
    closed_at: datetime | None
    close_reason: str | None
    imported_at: datetime
    last_seen_at: datetime
    asset: AssetSummary | None
    blocked_actions: list[BlockedAction]


async def comment_counts(session: AsyncSession, lease_ids: Sequence[int]) -> dict[int, int]:
    if not lease_ids:
        return {}
    rows = await session.execute(
        select(LeaseReturnComment.lease_return_id, func.count())
        .where(LeaseReturnComment.lease_return_id.in_(lease_ids))
        .group_by(LeaseReturnComment.lease_return_id)
    )
    return {lease_id: int(count) for lease_id, count in rows.all()}


def _list_fields(lease: LeaseReturn, available: list[str], comments: int) -> dict[str, object]:
    sr = lease.service_request
    return {
        "id": lease.id,
        "asset_tag": lease.asset_tag,
        "serial_number": lease.serial_number,
        "model": lease.model,
        "model_category": lease.model_category,
        "retirement_date": lease.retirement_date,
        "days_until_retirement": (lease.retirement_date - business_today()).days,
        "contact": contact_ref(lease.contact),
        "user_id": lease.user_id,
        "status": lease.status,
        "status_changed_at": lease.status_changed_at,
        "is_vip": lease.is_vip,
        "is_campus": lease.is_campus,
        "communication_date": lease.communication_date,
        "communication_paused": lease.communication_paused,
        "communication_resume_date": lease.communication_resume_date,
        "service_request": LeaseSrRef(id=sr.id, number=sr.number, status=sr.status) if sr else None,
        "comment_count": comments,
        "attention": lease_attention(lease),
        "available_actions": available,
    }


async def build_lease_list_items(
    session: AsyncSession, leases: Sequence[LeaseReturn], user: User | None
) -> list[LeaseReturnListItem]:
    counts = await comment_counts(session, [lease.id for lease in leases])
    return [
        LeaseReturnListItem(
            **_list_fields(
                lease, await actions.available_names(session, lease, user), counts.get(lease.id, 0)
            )  # type: ignore[arg-type]
        )
        for lease in leases
    ]


async def build_lease_detail(
    session: AsyncSession, lease: LeaseReturn, user: User | None
) -> LeaseReturnDetail:
    available, blocked = await actions.available(session, lease, user)
    counts = await comment_counts(session, [lease.id])
    asset = (await session.scalars(select(Asset).where(Asset.asset_tag == lease.asset_tag))).first()
    return LeaseReturnDetail(
        **_list_fields(lease, available, counts.get(lease.id, 0)),  # type: ignore[arg-type]
        version=lease.version,
        checked_in_at=lease.checked_in_at,
        install_status=lease.install_status,
        ci_name=lease.ci_name,
        cost_center=lease.cost_center,
        communication_pause_reason=lease.communication_pause_reason,
        info_sent_at=lease.info_sent_at,
        reminder_1_sent_at=lease.reminder_1_sent_at,
        reminder_2_sent_at=lease.reminder_2_sent_at,
        escalation_sent_at=lease.escalation_sent_at,
        escalation_email=lease.escalation_email,
        linked_at=lease.linked_at,
        closed_at=lease.closed_at,
        close_reason=lease.close_reason,
        imported_at=lease.imported_at,
        last_seen_at=lease.last_seen_at,
        asset=asset_summary(asset),
        blocked_actions=blocked,
    )
