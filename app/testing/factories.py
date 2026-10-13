"""Test factories for core entities (usable by slice tests too).

They rely on the seeded reference data (sites, regions, locations, articles, personas)
that the root ``conftest.py`` commits once per test session.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    AssetSource,
    AssetStatus,
    DeliveryType,
    ItemSlot,
    LeaseReturnStatus,
    ProcurementType,
    ServiceRequestStatus,
)
from app.core.models import (
    Article,
    Asset,
    Contact,
    LeaseReturn,
    Region,
    ServiceRequest,
    ServiceRequestItem,
    Site,
    StockLocation,
    User,
)
from app.core.timeutil import business_today, now_utc

_seq = itertools.count(1)


def _n() -> int:
    return next(_seq)


async def region(session: AsyncSession, code: str = "riverton") -> Region:
    return (await session.scalars(select(Region).where(Region.code == code))).one()


async def site(session: AsyncSession, code: str = "RIV") -> Site:
    return (await session.scalars(select(Site).where(Site.code == code))).one()


async def location(session: AsyncSession, code: str) -> StockLocation:
    return (await session.scalars(select(StockLocation).where(StockLocation.code == code))).one()


async def article(session: AsyncSession, device_type: str = "Laptop Standard 14") -> Article:
    stmt = select(Article).where(Article.internal_description == device_type, Article.is_active)
    return (await session.scalars(stmt)).one()


async def user(session: AsyncSession, email: str = "dana.dispatcher@example.com") -> User:
    return (await session.scalars(select(User).where(User.email == email))).one()


async def make_contact(
    session: AsyncSession,
    *,
    email: str | None = "test.person@example.com",
    zip_: str = "10010",
    city: str = "Riverton",
) -> Contact:
    n = _n()
    contact = Contact(
        user_id=f"T{900000 + n}",
        first_name="Test",
        last_name=f"Person{n}",
        email=email,
        zip=zip_,
        city=city,
        street=f"{n} Test Street",
    )
    session.add(contact)
    await session.flush()
    return contact


async def make_asset(
    session: AsyncSession,
    *,
    status: AssetStatus = AssetStatus.AVAILABLE,
    location_code: str = "RIV-STAGING",
    device_type: str = "Laptop Standard 14",
    place: str | None = "Z9-01",
    sr: ServiceRequest | None = None,
) -> Asset:
    n = _n()
    loc = await location(session, location_code)
    art = await article(session, device_type)
    asset = Asset(
        serial_number=f"TS{n:08d}",
        asset_tag=f"TT{n:06d}",
        article=art,
        article_id=art.id,
        model=art.model_number,
        status=status,
        location=loc,
        location_id=loc.id,
        installation_place=place if loc.type == "staging" else None,
        service_request=sr,
        service_request_id=sr.id if sr else None,
        source=AssetSource.MANUAL,
        checked_in_at=now_utc() - timedelta(days=10),
    )
    session.add(asset)
    await session.flush()
    return asset


async def make_sr(
    session: AsyncSession,
    *,
    status: ServiceRequestStatus | None = ServiceRequestStatus.IMPORTED,
    region_code: str | None = "riverton",
    campus: bool = False,
    delivery_type: DeliveryType | None = None,
    new_types: tuple[str, ...] = ("Laptop Standard 14",),
    old_tags: tuple[str, ...] = (),
    email: str | None = "test.person@example.com",
    appointment: datetime | None = None,
    editor: User | None = None,
    assign_new: bool = False,
    packed: bool = False,
    **fields: object,
) -> ServiceRequest:
    """An SR in ``status`` (``None`` = not yet imported) satisfying the core guards on request."""
    n = _n()
    contact = await make_contact(session, email=email)
    reg = await region(session, region_code) if region_code else None
    now = now_utc()
    values: dict[str, object] = {
        "number": f"SR{9000000 + n:07d}",
        "task_number": f"TASK{9000000 + n:07d}",
        "contact": contact,
        "contact_id": contact.id,
        "procurement_type": ProcurementType.REPLACEMENT,
        "region": reg,
        "region_id": reg.id if reg else None,
        "supplying_site": reg.supplying_site if reg else None,
        "supplying_site_id": reg.supplying_site_id if reg else None,
        "delivery_type": delivery_type or (reg.default_delivery_type if reg else None),
        "is_campus": campus,
        "status": status,
        "imported_at": now - timedelta(days=2),
        "last_imported_at": now - timedelta(days=2),
        "appointment_start_at": appointment,
        "appointment_end_at": appointment + timedelta(hours=1) if appointment else None,
        "editor": editor,
        "editor_user_id": editor.id if editor else None,
        "delivery_zip": "10010",
        "delivery_city": "Riverton",
        "items": [],
    }
    values.update(fields)
    sr = ServiceRequest(**values)
    slots = (ItemSlot.NEW_BASE, ItemSlot.NEW_PERIPHERAL_1, ItemSlot.NEW_PERIPHERAL_2)
    for slot, device_type in zip(slots, new_types, strict=False):
        art = await article(session, device_type)
        sr.items.append(
            ServiceRequestItem(slot=slot, requested_type=device_type, article_id=art.id)
        )
    for slot, tag in zip((ItemSlot.OLD_BASE, ItemSlot.OLD_PERIPHERAL_1), old_tags, strict=False):
        sr.items.append(ServiceRequestItem(slot=slot, expected_asset_tag=tag))
    session.add(sr)
    if status is None:
        return sr  # not flushed: import_create sets the status
    await session.flush()
    if assign_new:
        for item in sr.new_items:
            asset = await make_asset(session, status=AssetStatus.ASSIGNED, sr=sr)
            item.asset = asset
            item.asset_id = asset.id
            if packed:
                item.packed_at = now
        await session.flush()
    return sr


async def make_lease(
    session: AsyncSession,
    *,
    status: LeaseReturnStatus | None = LeaseReturnStatus.IMPORTED,
    email: str | None = "test.person@example.com",
    sr: ServiceRequest | None = None,
    **fields: object,
) -> LeaseReturn:
    n = _n()
    contact = await make_contact(session, email=email)
    now = now_utc()
    lease = LeaseReturn(
        asset_tag=f"TL{n:06d}",
        retirement_date=business_today() + timedelta(days=30),
        user_id=contact.user_id,
        contact=contact,
        contact_id=contact.id,
        status=status,
        imported_at=now - timedelta(days=5),
        last_seen_at=now - timedelta(days=5),
        service_request=sr,
        service_request_id=sr.id if sr else None,
        **fields,
    )
    session.add(lease)
    if status is not None:
        await session.flush()
    return lease
