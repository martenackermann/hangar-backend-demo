"""Parcel shipments via the ParcelCarrier gateway (SPEC §6.17, §8.2; API.md §6.6).

``ship`` is never dispatched through the generic action endpoint (SPEC_RESOLUTIONS #15):
:func:`create_shipment` checks the transition first, calls the carrier while nothing is
written yet (so a ``502 carrier_error`` leaves no trace), then applies ``ship`` and its
effects in the request's transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assets import asset_service
from app.core.enums import AssetAction, AssetStatus, DeliveryType, Trigger
from app.core.errors import (
    ApiError,
    GatewayError,
    GuardFailed,
    NotFoundError,
    TransitionNotAllowed,
)
from app.core.files import store_file
from app.core.gateways.protocols import (
    CarrierError,
    Parcel,
    ParcelCarrier,
    PostalAddress,
    ShipmentRequest,
    ShipmentResult,
)
from app.core.models import ServiceRequest, ServiceRequestItem, Site, User
from app.core.notifications import notify
from app.core.read_models import sr_ref, user_ref
from app.core.schemas import Address, EntityRef
from app.core.statemachine import guards
from app.core.timeutil import now_utc
from app.core.transitions import check_version, transition_service

from ..models import Shipment
from ..schemas import ShipmentCreate, ShipmentOut, ShipTo
from .fulfilment import DEFAULT_COUNTRY, ship_to

TEMPLATE_ID = "sr.shipping_confirmation"
MIN_COMBINED_KG = Decimal("0.5")
RETURN_PARCEL_KG = Decimal("2.0")
DEFAULT_KG = Decimal("1.0")


@dataclass(slots=True)
class PreparedShipment:
    request: ShipmentRequest
    ship_to: ShipTo
    include_return_label: bool
    warnings: list[str] = field(default_factory=list)


def _sender(site: Site, sr: ServiceRequest) -> PostalAddress:
    return PostalAddress(
        name=f"Acme IT Services · {site.name}",
        name2=f"Rollout for Globex Fleet AG · {sr.number}",
        street=site.street or "",
        zip=site.zip or "",
        city=site.city or site.name,
        country=site.country or DEFAULT_COUNTRY,
        email=None,
    )


def _item_weight(item: ServiceRequestItem) -> Decimal:
    article = item.asset.article if item.asset is not None else None
    article = article or item.article
    return Decimal(article.weight_kg) if article is not None else DEFAULT_KG


def _item_reference(item: ServiceRequestItem) -> str:
    if item.asset is not None:
        return item.asset.asset_tag or item.asset.serial_number
    return item.requested_type or str(item.slot)


def build_request(
    sr: ServiceRequest, *, include_return_label: bool, combined_parcel: bool
) -> PreparedShipment:
    """The carrier request for ``sr`` (pure; raises 409 when no address can be built)."""
    site = sr.supplying_site
    if site is None:
        raise ApiError(f"{sr.number} has no supplying hub.", status=409, code="conflict")
    target = ship_to(sr, site)
    address = target.address
    if not address.street or not address.zip or not address.city:
        raise ApiError(
            f"{sr.number} has no complete delivery address for a parcel.",
            status=409,
            code="conflict",
        )
    contact = sr.contact
    recipient = PostalAddress(
        name=target.recipient_name,
        name2=target.name2,
        street=address.street,
        zip=address.zip,
        city=address.city,
        country=address.country or DEFAULT_COUNTRY,
        email=contact.email if contact else None,
    )
    new_items = sr.new_items
    if combined_parcel:
        weight = sum((_item_weight(i) for i in new_items), Decimal(0))
        parcels = [
            Parcel(
                weight_kg=max(weight, MIN_COMBINED_KG),
                references=[sr.number, *(_item_reference(i) for i in new_items)],
            )
        ]
    else:
        parcels = [
            Parcel(weight_kg=_item_weight(i), references=[_item_reference(i), sr.number])
            for i in new_items
        ]
    warnings: list[str] = []
    return_parcels: list[Parcel] = []
    if include_return_label:
        old_items = sr.old_items
        if old_items:
            return_parcels = [
                Parcel(
                    weight_kg=RETURN_PARCEL_KG,
                    references=[
                        i.expected_asset_tag
                        or (i.asset.asset_tag if i.asset and i.asset.asset_tag else sr.number),
                        sr.number,
                    ],
                )
                for i in old_items
            ]
        else:
            warnings.append("no_return_items")
    sender = _sender(site, sr)
    request = ShipmentRequest(
        reference=sr.number,
        sender=sender,
        recipient=recipient,
        parcels=parcels,
        return_parcels=return_parcels,
        return_address=sender if return_parcels else None,
    )
    return PreparedShipment(request, target, bool(return_parcels), warnings)


async def _ensure_ship_allowed(session: AsyncSession, sr: ServiceRequest, actor: User) -> None:
    """Fail fast with the transition's own 409s before talking to the carrier."""
    if transition_service.candidate(sr, "ship", actor, Trigger.USER) is None:
        raise TransitionNotAllowed(
            f"Action 'ship' is not allowed while the status is '{sr.status}'.",
            action="ship",
            current_status=sr.status,
            allowed_actions=transition_service.allowed_actions(sr, actor),
        )
    for guard in guards.for_action("service_request", "ship"):
        if not guard.applies(Trigger.USER, evaluating_availability=False):
            continue
        failure = await guard.fn(session, sr, actor, None)
        if failure is not None:
            raise GuardFailed(failure.code, failure.message)


async def create_shipment(
    session: AsyncSession,
    sr: ServiceRequest,
    body: ShipmentCreate,
    actor: User,
    carrier: ParcelCarrier,
) -> tuple[Shipment, list[str]]:
    check_version(sr, body.version)
    await _ensure_ship_allowed(session, sr, actor)
    prepared = build_request(
        sr, include_return_label=body.include_return_label, combined_parcel=body.combined_parcel
    )
    try:
        result = await carrier.create_shipment(prepared.request)
    except CarrierError as exc:
        raise GatewayError(exc.message, code="carrier_error", title="Carrier error") from exc

    await transition_service.apply(
        session,
        sr,
        "ship",
        actor=actor,
        trigger=Trigger.USER,
        payload={
            "includeReturnLabel": prepared.include_return_label,
            "combinedParcel": body.combined_parcel,
            "sendConfirmation": body.send_confirmation,
            "carrierShipmentId": result.carrier_shipment_id,
        },
    )
    shipment, warnings = await record_shipment(
        session,
        sr,
        prepared,
        result,
        actor=actor,
        combined_parcel=body.combined_parcel,
        send_confirmation=body.send_confirmation,
    )
    for item in sr.new_items:
        asset = item.asset
        if asset is not None and asset.status == AssetStatus.PREPARED:
            await asset_service.move(
                session, asset, AssetAction.PROVIDE, actor=actor, trigger=Trigger.SYSTEM
            )
    sr.shipped_at = now_utc()
    await session.flush()
    return shipment, warnings


async def record_shipment(
    session: AsyncSession,
    sr: ServiceRequest,
    prepared: PreparedShipment,
    result: ShipmentResult,
    *,
    actor: User,
    combined_parcel: bool,
    send_confirmation: bool,
    mail: bool = True,
) -> tuple[Shipment, list[str]]:
    """Store label + ``shipments`` row and queue the confirmation (shared with the seeder)."""
    warnings = list(prepared.warnings)
    label = await store_file(
        session,
        result.label_pdf,
        f"label-{sr.number}-{result.carrier_shipment_id}.pdf",
        "application/pdf",
        "label",
        entity=("service_request", sr.id),
        created_by=actor,
    )
    contact = sr.contact
    confirmation = send_confirmation and contact is not None and bool(contact.email)
    if send_confirmation and not confirmation:
        warnings.append("no_contact_email")
    address = prepared.ship_to.address
    shipment = Shipment(
        service_request_id=sr.id,
        carrier_shipment_id=result.carrier_shipment_id,
        parcel_count=len(prepared.request.parcels),
        tracking_urls=list(result.tracking_urls),
        return_tracking_urls=list(result.return_tracking_urls),
        include_return_label=prepared.include_return_label,
        combined_parcel=combined_parcel,
        confirmation_mail_sent=confirmation,
        recipient_name=prepared.ship_to.recipient_name,
        delivery_street=address.street or "",
        delivery_zip=address.zip or "",
        delivery_city=address.city or "",
        delivery_country=address.country or DEFAULT_COUNTRY,
        label_file_id=label.id,
        created_by=actor.id,
    )
    session.add(shipment)
    await session.flush()
    if confirmation and mail and contact is not None and contact.email:
        await notify(
            session,
            TEMPLATE_ID,
            to=[contact.email],
            context=confirmation_context(sr, shipment, prepared.ship_to),
            entity=EntityRef(type="service_request", id=sr.id, label=sr.number),
            trigger=Trigger.USER,
            actor=actor,
        )
    return shipment, warnings


def confirmation_context(sr: ServiceRequest, shipment: Shipment, target: ShipTo) -> dict[str, Any]:
    contact = sr.contact
    address = target.address
    return {
        "sr_number": sr.number,
        "gender": str(contact.gender) if contact else "unspecified",
        "first_name": contact.first_name if contact else None,
        "last_name": contact.last_name if contact else sr.number,
        "tracking_urls": list(shipment.tracking_urls),
        "return_tracking_urls": list(shipment.return_tracking_urls),
        "pickup_point": target.kind == "pickup_point",
        "recipient_name": target.recipient_name,
        "name2": target.name2,
        "street": address.street,
        "zip": address.zip,
        "city": address.city,
        "country": address.country,
        "appointment_start_at": sr.appointment_start_at,
        "carrier_shipment_id": shipment.carrier_shipment_id,
        "items": [
            {
                "name": i.asset.article.name if i.asset and i.asset.article else i.requested_type,
                "tag": (i.asset.asset_tag or i.asset.serial_number) if i.asset else None,
            }
            for i in sr.new_items
        ],
    }


async def preview_context(session: AsyncSession, entity_id: int) -> dict[str, Any]:
    """``build_context`` of the template: the SR's latest shipment (template preview)."""
    sr = await session.get(ServiceRequest, entity_id)
    if sr is None:
        raise NotFoundError(f"Service request {entity_id} not found.")
    shipment = (
        await session.scalars(
            select(Shipment)
            .where(Shipment.service_request_id == sr.id)
            .order_by(Shipment.created_at.desc(), Shipment.id.desc())
            .limit(1)
        )
    ).first()
    if shipment is None:
        raise NotFoundError(f"{sr.number} has no shipment yet.")
    return confirmation_context(sr, shipment, ship_to(sr, sr.supplying_site))


def shipment_out(shipment: Shipment) -> ShipmentOut:
    sr = sr_ref(shipment.service_request)
    creator = user_ref(shipment.creator)
    assert sr is not None
    assert creator is not None
    return ShipmentOut(
        id=shipment.id,
        service_request=sr,
        carrier_shipment_id=shipment.carrier_shipment_id,
        parcel_count=shipment.parcel_count,
        tracking_urls=list(shipment.tracking_urls),
        return_tracking_urls=list(shipment.return_tracking_urls),
        include_return_label=shipment.include_return_label,
        combined_parcel=shipment.combined_parcel,
        confirmation_mail_sent=shipment.confirmation_mail_sent,
        recipient_name=shipment.recipient_name,
        delivery_address=Address(
            street=shipment.delivery_street,
            zip=shipment.delivery_zip,
            city=shipment.delivery_city,
            country=shipment.delivery_country,
        ),
        created_by=creator,
        created_at=shipment.created_at,
    )


__all__ = [
    "DeliveryType",
    "PreparedShipment",
    "build_request",
    "confirmation_context",
    "create_shipment",
    "preview_context",
    "record_shipment",
    "shipment_out",
]
