"""Contact response models + builders (API.md §4.6; ``ContactSummary`` = ``ContactListItem``)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    LR_NON_TERMINAL,
    SR_NON_TERMINAL,
    Gender,
    LeaseReturnStatus,
    ServiceRequestStatus,
)
from app.core.models import Contact, LeaseReturn, ServiceRequest, VipEntry
from app.core.schemas import ApiModel


class ContactListItem(ApiModel):
    id: int
    user_id: str
    display_name: str
    email: str | None
    department: str | None
    city: str | None
    is_vip: bool
    open_service_requests: int
    open_lease_returns: int


ContactSummary = ContactListItem


class ContactAddress(ApiModel):
    street: str | None
    zip: str | None
    city: str | None
    building: str | None
    room: str | None


class ContactVip(ApiModel):
    vip_type: str
    title: str | None


class ContactSrRef(ApiModel):
    id: int
    number: str
    status: ServiceRequestStatus


class ContactLeaseRef(ApiModel):
    id: int
    asset_tag: str
    status: LeaseReturnStatus


class ContactDetail(ContactListItem):
    gender: Gender
    first_name: str | None
    last_name: str
    phone: str | None
    mobile: str | None
    address: ContactAddress
    is_active: bool
    vip: ContactVip | None
    service_requests: list[ContactSrRef]
    lease_returns: list[ContactLeaseRef]


async def _counts(
    session: AsyncSession, contact_ids: Sequence[int]
) -> tuple[dict[int, int], dict[int, int]]:
    if not contact_ids:
        return {}, {}
    srs = await session.execute(
        select(ServiceRequest.contact_id, func.count())
        .where(
            ServiceRequest.contact_id.in_(contact_ids), ServiceRequest.status.in_(SR_NON_TERMINAL)
        )
        .group_by(ServiceRequest.contact_id)
    )
    leases = await session.execute(
        select(LeaseReturn.contact_id, func.count())
        .where(LeaseReturn.contact_id.in_(contact_ids), LeaseReturn.status.in_(LR_NON_TERMINAL))
        .group_by(LeaseReturn.contact_id)
    )
    return (
        {cid: int(n) for cid, n in srs.all() if cid is not None},
        {cid: int(n) for cid, n in leases.all() if cid is not None},
    )


async def _vips(session: AsyncSession, user_ids: Sequence[str]) -> dict[str, VipEntry]:
    if not user_ids:
        return {}
    rows = (await session.scalars(select(VipEntry).where(VipEntry.user_id.in_(user_ids)))).all()
    return {row.user_id: row for row in rows}


async def build_contact_summaries(
    session: AsyncSession, contacts: Sequence[Contact]
) -> list[ContactListItem]:
    open_srs, open_leases = await _counts(session, [c.id for c in contacts])
    vips = await _vips(session, [c.user_id for c in contacts])
    return [
        ContactListItem(
            id=c.id,
            user_id=c.user_id,
            display_name=c.display_name,
            email=c.email,
            department=c.department,
            city=c.city,
            is_vip=c.user_id in vips,
            open_service_requests=open_srs.get(c.id, 0),
            open_lease_returns=open_leases.get(c.id, 0),
        )
        for c in contacts
    ]


async def build_contact_detail(session: AsyncSession, contact: Contact) -> ContactDetail:
    [summary] = await build_contact_summaries(session, [contact])
    vip = (await _vips(session, [contact.user_id])).get(contact.user_id)
    srs = (
        await session.scalars(
            select(ServiceRequest)
            .where(ServiceRequest.contact_id == contact.id)
            .order_by(ServiceRequest.number.desc())
        )
    ).all()
    leases = (
        await session.scalars(
            select(LeaseReturn)
            .where(LeaseReturn.contact_id == contact.id)
            .order_by(LeaseReturn.asset_tag)
        )
    ).all()
    return ContactDetail(
        **summary.model_dump(by_alias=False),
        gender=contact.gender,
        first_name=contact.first_name,
        last_name=contact.last_name,
        phone=contact.phone,
        mobile=contact.mobile,
        address=ContactAddress(
            street=contact.street,
            zip=contact.zip,
            city=contact.city,
            building=contact.building,
            room=contact.room,
        ),
        is_active=contact.is_active,
        vip=ContactVip(vip_type=vip.vip_type, title=vip.title) if vip else None,
        service_requests=[ContactSrRef(id=s.id, number=s.number, status=s.status) for s in srs],
        lease_returns=[
            ContactLeaseRef(id=item_.id, asset_tag=item_.asset_tag, status=item_.status)
            for item_ in leases
        ],
    )
