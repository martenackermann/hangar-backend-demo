"""Order closing queue, technician picking, parcel picking and packing scans (API.md §6.3–6.5)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from sqlalchemy import ColumnElement, Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    PARCEL_DELIVERY,
    DeliveryType,
    ItemSlot,
    ServiceRequestStatus,
)
from app.core.errors import GuardFailed
from app.core.models import ServiceRequest, Site, User
from app.core.read_models import ServiceRequestItemOut, build_item, build_sr_list_item, site_ref
from app.core.rules import software_checklist
from app.core.schemas import Address, ScanResult, UndoLink
from app.core.timeutil import now_utc

from ..schemas import (
    OrderClosingQueueItem,
    ParcelPickingItem,
    PickingCalendar,
    PickingTotals,
    ShipTo,
    SoftwareChecklistOut,
    TechnicianPicking,
    TechnicianPickingGroup,
    TechnicianPickingRow,
)
from .common import (
    business_date,
    conflict,
    find_asset,
    is_delivery,
    item_for,
    require_new_slot,
    scan_result,
    slot_label,
    sr_search_clause,
)
from .personalization import appointment_range, region_clauses

SR = ServiceRequestStatus
DEFAULT_COUNTRY = "Freeland"

#: SPEC §6.15 in SQL: every required entry (requested ∪ added) is in ``installed``.
_CHECKLIST_SQL = (
    "NOT EXISTS (SELECT 1 FROM unnest(service_requests.requested_software "
    "|| service_requests.added_software) AS req(v) WHERE btrim(req.v) <> '' "
    "AND lower(btrim(req.v)) NOT IN (SELECT lower(btrim(ins.v)) "
    "FROM unnest(service_requests.installed_software) AS ins(v)))"
)
CHECKLIST_COMPLETE = text(_CHECKLIST_SQL)
CHECKLIST_INCOMPLETE = text(f"NOT ({_CHECKLIST_SQL})")


def checklist_out(sr: ServiceRequest) -> SoftwareChecklistOut:
    c = software_checklist(sr.requested_software, sr.added_software, sr.installed_software)
    return SoftwareChecklistOut(
        required=c.required,
        installed=c.installed,
        added=c.added,
        missing=c.missing,
        complete=c.complete,
    )


# --- order closing -------------------------------------------------------------------------------


def order_closing_stmt(
    *,
    site_id: int,
    region_ids: Sequence[int | str] | None,
    appointment_from: date | None,
    appointment_to: date | None,
    checklist_complete: bool | None,
    q: str | None,
) -> Select[ServiceRequest]:
    clauses: list[ColumnElement[bool]] = [
        ServiceRequest.status == SR.PERSONALIZATION_DONE,
        ServiceRequest.supplying_site_id == site_id,
        *region_clauses(region_ids),
        *appointment_range(appointment_from, appointment_to),
    ]
    if checklist_complete is True:
        clauses.append(CHECKLIST_COMPLETE)  # type: ignore[arg-type]
    elif checklist_complete is False:
        clauses.append(CHECKLIST_INCOMPLETE)  # type: ignore[arg-type]
    search = sr_search_clause(q)
    if search is not None:
        clauses.append(search)
    return select(ServiceRequest).where(*clauses)


async def build_order_closing_items(
    session: AsyncSession, srs: Sequence[ServiceRequest], user: User
) -> list[OrderClosingQueueItem]:
    return [
        OrderClosingQueueItem(
            **(await build_sr_list_item(session, sr, user)).model_dump(by_alias=False),
            items=[build_item(i) for i in sr.items],
            checklist=checklist_out(sr),
            os_reinstall=sr.os_reinstall,
        )
        for sr in srs
    ]


# --- technician picking ---------------------------------------------------------------------------


async def technician_picking(
    session: AsyncSession, *, site: Site, day: date, user: User
) -> TechnicianPicking:
    stmt = (
        select(ServiceRequest)
        .where(
            ServiceRequest.status.in_([SR.READY_FOR_PICKING, SR.PROVIDED]),
            ServiceRequest.supplying_site_id == site.id,
            is_delivery(DeliveryType.TECHNICIAN),
            business_date(ServiceRequest.appointment_start_at) == day,
        )
        .order_by(
            ServiceRequest.appointment_calendar_name.asc().nulls_last(),
            ServiceRequest.appointment_start_at.asc(),
            ServiceRequest.id.asc(),
        )
    )
    srs = (await session.scalars(stmt)).all()
    groups: dict[tuple[int | None, str | None], list[TechnicianPickingRow]] = {}
    for sr in srs:
        base = await build_sr_list_item(session, sr, user)
        row = TechnicianPickingRow(
            **base.model_dump(by_alias=False),
            items=[build_item(i) for i in sr.items],
            picked=sr.status == SR.PROVIDED,
        )
        key = (sr.appointment_calendar_id, sr.appointment_calendar_name)
        groups.setdefault(key, []).append(row)
    site_out = site_ref(site)
    assert site_out is not None
    picked = sum(1 for sr in srs if sr.status == SR.PROVIDED)
    return TechnicianPicking(
        date=day,
        site=site_out,
        groups=[
            TechnicianPickingGroup(
                calendar=PickingCalendar(id=calendar_id, name=name), service_requests=rows
            )
            for (calendar_id, name), rows in groups.items()
        ],
        totals=PickingTotals(to_pick=len(srs) - picked, picked=picked),
    )


# --- parcel picking + packing ---------------------------------------------------------------------


def ship_to(sr: ServiceRequest, site: Site | None = None) -> ShipTo:
    """Recipient of a parcel (SPEC §6.17): user address, or the region's pickup point."""
    contact_name = sr.contact.display_name if sr.contact else sr.number
    country = sr.delivery_country or (site.country if site else None) or DEFAULT_COUNTRY
    region = sr.region
    if sr.effective_delivery_type == DeliveryType.PARCEL_TO_PICKUP_POINT and region is not None:
        return ShipTo(
            kind="pickup_point",
            recipient_name=region.pickup_point_name or f"{region.name} pickup point",
            name2=f"Attn: {contact_name}",
            address=Address(
                street=region.pickup_point_street,
                zip=region.pickup_point_zip,
                city=region.pickup_point_city,
                country=country,
            ),
        )
    contact = sr.contact
    street = sr.delivery_street or (contact.street if contact else None)
    zip_ = sr.delivery_zip or (contact.zip if contact else None)
    city = sr.delivery_city or (contact.city if contact else None)
    line2 = ", ".join(
        part
        for part in (
            f"Room {sr.delivery_room}" if sr.delivery_room else None,
            f"Building {sr.delivery_building}" if sr.delivery_building else None,
        )
        if part
    )
    return ShipTo(
        kind="user",
        recipient_name=contact_name,
        name2=line2 or None,
        address=Address(street=street, zip=zip_, city=city, country=country),
    )


def parcel_picking_stmt(
    *,
    site_id: int | None,
    delivery_types: Sequence[DeliveryType] | None,
    q: str | None,
    appointment_to: date | None,
) -> Select[ServiceRequest]:
    types = [t for t in (delivery_types or PARCEL_DELIVERY) if t in PARCEL_DELIVERY]
    clauses: list[ColumnElement[bool]] = [
        ServiceRequest.status == SR.READY_FOR_PICKING,
        is_delivery(*(types or PARCEL_DELIVERY)),
        *appointment_range(None, appointment_to),
    ]
    if site_id is not None:
        clauses.append(ServiceRequest.supplying_site_id == site_id)
    search = sr_search_clause(q)
    if search is not None:
        clauses.append(search)
    return (
        select(ServiceRequest)
        .where(*clauses)
        .order_by(ServiceRequest.appointment_start_at.asc().nulls_last(), ServiceRequest.id.asc())
    )


async def build_parcel_items(
    session: AsyncSession, srs: Sequence[ServiceRequest], user: User
) -> list[ParcelPickingItem]:
    result: list[ParcelPickingItem] = []
    for sr in srs:
        base = await build_sr_list_item(session, sr, user)
        result.append(
            ParcelPickingItem(
                **base.model_dump(by_alias=False),
                items=[build_item(i) for i in sr.items],
                all_packed=all(i.packed_at is not None for i in sr.new_items),
                has_old_items=bool(sr.old_items),
                ship_to=ship_to(sr, sr.supplying_site),
            )
        )
    return result


def _check_packing(sr: ServiceRequest) -> None:
    if sr.status != SR.READY_FOR_PICKING:
        raise conflict(f"{sr.number} is not ready for packing (status '{sr.status}').")
    if sr.effective_delivery_type not in PARCEL_DELIVERY:
        raise GuardFailed("delivery_type_is_parcel", "This request is delivered by a technician.")


async def pack_by_scan(
    session: AsyncSession, sr: ServiceRequest, serial: str, actor: User
) -> ScanResult[ServiceRequestItemOut]:
    _check_packing(sr)
    scanned = serial.strip().upper()

    def progress() -> tuple[int, int]:
        items = sr.new_items
        return sum(1 for i in items if i.packed_at is not None), len(items)

    asset = await find_asset(session, serial)
    if asset is None:
        return scan_result(
            "unknown_serial",
            f"Serial {scanned} is not in stock records.",
            scanned,
            progress=progress(),
        )
    scanned = asset.serial_number
    item = next((i for i in sr.new_items if i.asset_id == asset.id), None)
    if item is None:
        return scan_result(
            "not_on_this_request",
            f"{asset.asset_tag or scanned} is not assigned to {sr.number}.",
            scanned,
            progress=progress(),
        )
    undo = UndoLink(
        method="DELETE", href=f"/service-requests/{sr.id}/items/{item.slot}/packing", body=None
    )
    device = asset.asset_tag or asset.serial_number
    if item.packed_at is not None:
        return scan_result(
            "already_packed",
            f"{device} ({slot_label(item.slot)}) is already packed.",
            scanned,
            entity=build_item(item),
            progress=progress(),
        )
    item.packed_at = now_utc()
    item.packed_by = actor.id
    await session.flush()
    await session.refresh(item, ["packed_by_user"])
    done, total = progress()
    name = asset.article.name if asset.article else (asset.model or "Device")
    return scan_result(
        "packed",
        f"{name} {device} packed ({done}/{total}).",
        scanned,
        applied=True,
        entity=build_item(item),
        undo=undo,
        progress=(done, total),
    )


async def unpack(
    session: AsyncSession, sr: ServiceRequest, slot: ItemSlot, actor: User
) -> ServiceRequestItemOut:
    require_new_slot(slot)
    item = item_for(sr, slot)
    _check_packing(sr)
    if item.packed_at is not None:
        item.packed_at = None
        item.packed_by = None
        await session.flush()
        await session.refresh(item, ["packed_by_user"])
    return build_item(item)
