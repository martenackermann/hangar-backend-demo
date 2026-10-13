"""⌘K search provider: a carrier shipment id (``PC…``) resolves to its service request."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ServiceRequestStatus, tone
from app.core.models import User
from app.core.search import SearchHit

from .models import Shipment

_SHIPMENT = re.compile(r"^PC[0-9]{8,14}(-[0-9]+R?)?$")


async def provider(session: AsyncSession, q: str, user: User) -> list[SearchHit]:
    code = q.strip().upper()
    if not _SHIPMENT.match(code):
        return []
    shipment_id = code.split("-", 1)[0]
    rows = (
        await session.scalars(
            select(Shipment).where(Shipment.carrier_shipment_id.startswith(shipment_id)).limit(5)
        )
    ).all()
    hits: list[SearchHit] = []
    for shipment in rows:
        sr = shipment.service_request
        status = ServiceRequestStatus(sr.status)
        hits.append(
            SearchHit(
                kind="service_request",
                id=sr.id,
                title=sr.number,
                subtitle=f"Shipment {shipment.carrier_shipment_id} · {shipment.recipient_name}",
                status=status.value,
                tone=tone(status),
                href=f"/service-requests?sr={sr.id}&tab=shipping",
                exact=shipment.carrier_shipment_id == shipment_id,
            )
        )
    return hits
