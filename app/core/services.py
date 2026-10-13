"""Core write helpers shared by slices (SPEC §2.4 "SR read-model setters", "Lease linking").

These are the only writers of the denormalized ``service_requests.appointment_*`` columns
and of the lease ↔ SR link columns, and they recompute VIP flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import LeaseReturnStatus, Trigger
from app.core.events import VipListChanged, emit
from app.core.models import Contact, LeaseReturn, ServiceRequest, User, VipEntry
from app.core.rules import normalize_asset_tag
from app.core.timeutil import now_utc
from app.core.transitions import LEASE_TRANSITIONS, transition_service

LR = LeaseReturnStatus
_LINK = LEASE_TRANSITIONS.get("link_service_request")
assert _LINK is not None
_LINKABLE = _LINK.from_


# --- appointment read model (H4 is the caller) ---------------------------------------------


def set_appointment(
    session: AsyncSession,
    sr: ServiceRequest,
    *,
    start_at: datetime,
    end_at: datetime,
    calendar_id: int | None,
    calendar_name: str | None,
) -> None:
    sr.appointment_start_at = start_at
    sr.appointment_end_at = end_at
    sr.appointment_calendar_id = calendar_id
    sr.appointment_calendar_name = calendar_name


def clear_appointment(session: AsyncSession, sr: ServiceRequest) -> None:
    sr.appointment_start_at = None
    sr.appointment_end_at = None
    sr.appointment_calendar_id = None
    sr.appointment_calendar_name = None


# --- lease ↔ SR linking (SPEC §6.4) -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinkWarning:
    asset_tag: str
    code: str
    message: str


async def link_lease_return(
    session: AsyncSession,
    lease: LeaseReturn,
    sr: ServiceRequest,
    *,
    actor: User | None,
    trigger: Trigger,
    comment: str | None = None,
) -> None:
    """Link + transition ``link_service_request`` (sets ``service_request_id``, ``linked_at``)."""
    lease.service_request = sr
    lease.service_request_id = sr.id
    lease.linked_at = now_utc()
    await transition_service.apply(
        session,
        lease,
        "link_service_request",
        actor=actor,
        trigger=trigger,
        payload={"serviceRequestNumber": sr.number},
        comment=comment,
    )


async def unlink_lease_return(
    session: AsyncSession,
    lease: LeaseReturn,
    *,
    actor: User | None,
    trigger: Trigger,
    comment: str | None = None,
) -> None:
    """Transition ``unlink_service_request`` to the restored status, then clear the link."""
    await transition_service.apply(
        session, lease, "unlink_service_request", actor=actor, trigger=trigger, comment=comment
    )
    lease.service_request = None
    lease.service_request_id = None
    lease.linked_at = None


async def link_lease_returns(
    session: AsyncSession, sr: ServiceRequest, trigger: Trigger, actor: User | None = None
) -> list[LinkWarning]:
    """Link every lease return named by an old item's expected asset tag (SPEC §6.4)."""
    warnings: list[LinkWarning] = []
    tags = [normalize_asset_tag(i.expected_asset_tag) for i in sr.old_items if i.expected_asset_tag]
    tags = [t for t in tags if t]
    if not tags:
        return warnings
    leases = (
        await session.scalars(select(LeaseReturn).where(LeaseReturn.asset_tag.in_(tags)))
    ).all()
    for lease in leases:
        if lease.status == LR.REPLACEMENT_ORDERED:
            if lease.service_request_id not in (None, sr.id):
                warnings.append(
                    LinkWarning(
                        lease.asset_tag,
                        "lease_already_linked",
                        f"Lease return {lease.asset_tag} is already linked to another service request.",
                    )
                )
            continue
        if lease.status in _LINKABLE:
            await link_lease_return(session, lease, sr, actor=actor, trigger=trigger)
    return warnings


# --- VIP flags (SPEC §5.1 vip_entries side effect) -----------------------------------------


async def recompute_vip(session: AsyncSession, user_id: str) -> None:
    """Refresh ``is_vip`` on SRs and lease returns of ``user_id`` (flag only, status unchanged)."""
    is_vip = (
        await session.scalar(select(VipEntry.id).where(VipEntry.user_id == user_id))
    ) is not None
    contact_ids = select(Contact.id).where(Contact.user_id == user_id).scalar_subquery()
    await session.execute(
        update(ServiceRequest)
        .where(ServiceRequest.contact_id.in_(contact_ids))
        .values(is_vip=is_vip)
        .execution_options(synchronize_session="fetch")
    )
    await session.execute(
        update(LeaseReturn)
        .where(LeaseReturn.user_id == user_id)
        .values(is_vip=is_vip)
        .execution_options(synchronize_session="fetch")
    )
    await emit(session, VipListChanged(user_id))


async def is_vip_user(session: AsyncSession, user_id: str) -> bool:
    return (
        await session.scalar(select(VipEntry.id).where(VipEntry.user_id == user_id))
    ) is not None
