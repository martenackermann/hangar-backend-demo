"""Picking, parcel packing and shipments (API.md §6.4–6.6)."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.enums import DeliveryType, ItemSlot
from app.core.errors import NotFoundError, problem_responses
from app.core.files import signed_url
from app.core.gateways import CarrierDep
from app.core.models import ServiceRequest, User
from app.core.pagination import Page, PageParamsDep, csv_enum, csv_ints, fetch_page
from app.core.read_models import ServiceRequestItemOut, build_sr_detail
from app.core.schemas import DownloadLink, ListResponse, ScanResult
from app.core.timeutil import business_today

from ..models import Shipment
from ..schemas import (
    ParcelPickingItem,
    ParcelScanIn,
    ShipmentCreate,
    ShipmentCreated,
    ShipmentOut,
    TechnicianPicking,
)
from ..services import fulfilment, shipping
from ..services.common import business_date, get_hub, get_sr

router = APIRouter(tags=["fulfilment"])

Reader = Annotated[User, Depends(require("sr.read"))]
Shipper = Annotated[User, Depends(require("fulfilment.ship"))]


@router.get(
    "/picking/technician",
    response_model=TechnicianPicking,
    summary="Technician picking list for a hub and date",
    responses=problem_responses(401, 403, 404, 422),
)
async def technician_picking(
    session: SessionDep,
    user: Reader,
    site_id: Annotated[int, Query(alias="siteId")],
    day: Annotated[date | None, Query(alias="date", description="Default: today")] = None,
) -> TechnicianPicking:
    site = await get_hub(session, site_id)
    return await fulfilment.technician_picking(
        session, site=site, day=day or business_today(), user=user
    )


@router.get(
    "/picking/parcel",
    response_model=ListResponse[ParcelPickingItem],
    summary="Parcel orders ready for packing",
    responses=problem_responses(401, 403, 404, 422),
)
async def parcel_picking(
    session: SessionDep,
    user: Reader,
    delivery_types: Annotated[
        list[DeliveryType] | None, Depends(csv_enum("deliveryType", DeliveryType))
    ],
    site_id: Annotated[int | None, Query(alias="siteId")] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    appointment_to: Annotated[date | None, Query(alias="appointmentTo")] = None,
) -> ListResponse[ParcelPickingItem]:
    if site_id is not None:
        await get_hub(session, site_id)
    stmt = fulfilment.parcel_picking_stmt(
        site_id=site_id, delivery_types=delivery_types, q=q, appointment_to=appointment_to
    )
    srs = (await session.scalars(stmt.limit(200))).all()
    return ListResponse[ParcelPickingItem](
        data=await fulfilment.build_parcel_items(session, srs, user)
    )


@router.post(
    "/scan/parcel",
    response_model=ScanResult[ServiceRequestItemOut],
    summary="Packing scan for a parcel order",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def scan_parcel(
    body: ParcelScanIn, session: SessionDep, user: Shipper
) -> ScanResult[ServiceRequestItemOut]:
    sr = await get_sr(session, body.service_request_id, for_update=True)
    result = await fulfilment.pack_by_scan(session, sr, body.serial, user)
    await commit(session)
    return result


@router.delete(
    "/service-requests/{sr_id}/items/{slot}/packing",
    response_model=ServiceRequestItemOut,
    summary="Remove the packed mark of a new item",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def unpack_item(
    sr_id: int, slot: ItemSlot, session: SessionDep, user: Shipper
) -> ServiceRequestItemOut:
    sr = await get_sr(session, sr_id, for_update=True)
    result = await fulfilment.unpack(session, sr, slot, user)
    await commit(session)
    return result


@router.post(
    "/service-requests/{sr_id}/shipments",
    response_model=ShipmentCreated,
    status_code=201,
    summary="Create a parcel shipment (transition ship)",
    responses=problem_responses(401, 403, 404, 409, 422, 502),
)
async def create_shipment(
    sr_id: int,
    session: SessionDep,
    user: Shipper,
    carrier: CarrierDep,
    body: ShipmentCreate | None = None,
) -> ShipmentCreated:
    sr = await get_sr(session, sr_id, for_update=True)
    shipment, warnings = await shipping.create_shipment(
        session, sr, body or ShipmentCreate(), user, carrier
    )
    await commit(session)
    await session.refresh(sr)
    await session.refresh(shipment)
    return ShipmentCreated(
        shipment=shipping.shipment_out(shipment),
        service_request=await build_sr_detail(session, sr, user),
        label=signed_url(shipment.label_file),
        warnings=warnings,
    )


@router.get(
    "/service-requests/{sr_id}/shipments",
    response_model=ListResponse[ShipmentOut],
    summary="Shipments of a service request",
    responses=problem_responses(401, 403, 404),
)
async def sr_shipments(sr_id: int, session: SessionDep, _user: Reader) -> ListResponse[ShipmentOut]:
    await get_sr(session, sr_id)
    rows = (
        await session.scalars(
            select(Shipment)
            .where(Shipment.service_request_id == sr_id)
            .order_by(Shipment.created_at.desc(), Shipment.id.desc())
        )
    ).all()
    return ListResponse[ShipmentOut](data=[shipping.shipment_out(s) for s in rows])


@router.get(
    "/shipments",
    response_model=Page[ShipmentOut],
    summary="Shipments (newest first)",
    responses=problem_responses(401, 403, 422),
)
async def list_shipments(
    session: SessionDep,
    _user: Reader,
    paging: PageParamsDep,
    site_ids: Annotated[list[int | str] | None, Depends(csv_ints("siteId"))],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[ShipmentOut]:
    stmt = select(Shipment).join(ServiceRequest, ServiceRequest.id == Shipment.service_request_id)
    if site_ids:
        stmt = stmt.where(ServiceRequest.supplying_site_id.in_([int(s) for s in site_ids]))
    created = business_date(Shipment.created_at)
    if date_from is not None:
        stmt = stmt.where(created >= date_from)
    if date_to is not None:
        stmt = stmt.where(created <= date_to)
    if q and q.strip():
        pattern = f"%{q.strip().replace('%', '').replace('_', '')}%"
        stmt = stmt.where(
            or_(
                ServiceRequest.number.ilike(pattern),
                Shipment.carrier_shipment_id.ilike(pattern),
                Shipment.recipient_name.ilike(pattern),
            )
        )
    stmt = stmt.order_by(Shipment.created_at.desc(), Shipment.id.desc())
    rows, meta = await fetch_page(session, stmt, paging)
    return Page[ShipmentOut](data=[shipping.shipment_out(s) for s in rows], meta=meta)


@router.get(
    "/shipments/{shipment_id}/label",
    response_model=DownloadLink,
    summary="Download link for a shipment's label PDF",
    responses=problem_responses(401, 403, 404),
)
async def shipment_label(shipment_id: int, session: SessionDep, _user: Shipper) -> DownloadLink:
    shipment = await session.get(Shipment, shipment_id)
    if shipment is None:
        raise NotFoundError(f"Shipment {shipment_id} not found.")
    return signed_url(shipment.label_file)
