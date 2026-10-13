"""H3 seeder (order 30): ``shipments`` + label PDFs for every SR the core story shipped.

The core seeder owns SR statuses, items, packing marks and asset movements; this seeder only
adds what the ``ship`` transition would have written to H3's own table (SPEC §11, §6.17).
Deterministic: carrier ids ``PC<ship date><900001…>`` (the live fake carrier numbers from its
Postgres sequence, far below), parcels and labels rendered like the fake carrier does.
"""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.gateways.fakes.carrier import TRACK_BASE, render_label
from app.core.gateways.protocols import ShipmentResult
from app.core.loading import ensure_loaded
from app.core.models import ServiceRequest, StatusTransition, User
from app.core.seed.framework import SeedContext
from app.core.timeutil import to_business_date

from .models import Shipment
from .services.shipping import build_request, record_shipment

SEED_SEQUENCE_START = 900_000
DANA = "dana.dispatcher@example.com"


async def seed_staging(session: AsyncSession, ctx: SeedContext) -> None:
    if (await session.scalars(select(Shipment.id).limit(1))).first() is not None:
        return
    shipped = (
        await session.execute(
            select(ServiceRequest, StatusTransition)
            .join(
                StatusTransition,
                (StatusTransition.entity_id == ServiceRequest.id)
                & (StatusTransition.entity_type == "service_request")
                & (StatusTransition.action == "ship"),
            )
            .where(~exists().where(Shipment.service_request_id == ServiceRequest.id))
            .order_by(StatusTransition.occurred_at, ServiceRequest.id)
        )
    ).all()
    fallback = await ctx.user(session, DANA)
    for n, (sr, transition) in enumerate(shipped, start=1):
        await ensure_loaded(session, sr)
        actor = (
            await session.get(User, transition.actor_user_id)
            if transition.actor_user_id is not None
            else None
        ) or fallback
        combined = ctx.rng.random() < 0.25
        try:
            prepared = build_request(
                sr, include_return_label=bool(sr.old_items), combined_parcel=combined
            )
        except ApiError:  # pragma: no cover - the core story always has hub + address
            continue
        if not prepared.request.parcels:  # pragma: no cover - shipped SRs carry assets
            continue
        day = to_business_date(transition.occurred_at)
        shipment_id = f"PC{day:%Y%m%d}{SEED_SEQUENCE_START + n:06d}"
        parcel_ids = [f"{shipment_id}-{i}" for i in range(1, len(prepared.request.parcels) + 1)]
        return_ids = [
            f"{shipment_id}-{len(parcel_ids) + i}"
            for i in range(1, len(prepared.request.return_parcels) + 1)
        ]
        result = ShipmentResult(
            carrier_shipment_id=shipment_id,
            parcel_ids=parcel_ids,
            tracking_urls=[f"{TRACK_BASE}/{pid}" for pid in parcel_ids],
            return_tracking_urls=[f"{TRACK_BASE}/return/{pid}R" for pid in return_ids],
            label_pdf=render_label(prepared.request, shipment_id, parcel_ids, return_ids),
        )
        shipment, _ = await record_shipment(
            session,
            sr,
            prepared,
            result,
            actor=actor,
            combined_parcel=combined,
            send_confirmation=True,
            mail=False,
        )
        shipment.created_at = transition.occurred_at
        shipment.updated_at = transition.occurred_at
    await session.flush()
