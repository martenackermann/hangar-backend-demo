"""H3 seeder + exporter: every SR the core story shipped has a shipment and a label."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import ServiceRequest, StatusTransition
from app.core.seed.framework import SeedContext, exporters
from app.modules.h3_staging.models import Shipment
from app.modules.h3_staging.seed import seed_staging
from app.modules.h3_staging.tests.helpers import SR


async def test_seeded_shipments_match_ship_transitions(db_session: AsyncSession) -> None:
    shipped_ids = set(
        (
            await db_session.scalars(
                select(StatusTransition.entity_id).where(
                    StatusTransition.entity_type == "service_request",
                    StatusTransition.action == "ship",
                )
            )
        ).all()
    )
    shipments = (await db_session.scalars(select(Shipment))).all()
    assert {s.service_request_id for s in shipments} == shipped_ids
    currently_shipped = set(
        (
            await db_session.scalars(
                select(ServiceRequest.id).where(ServiceRequest.status == SR.SHIPPED)
            )
        ).all()
    )
    assert len(currently_shipped) == 15
    assert currently_shipped <= shipped_ids
    for shipment in shipments:
        assert shipment.carrier_shipment_id.startswith("PC")
        assert len(shipment.tracking_urls) == shipment.parcel_count
        assert shipment.label_file.purpose == "label"
        assert shipment.label_file.content_type == "application/pdf"
    sample = shipments[0]
    assert sample.label_file.filename.startswith("label-")


async def test_seeder_is_idempotent(db_session: AsyncSession) -> None:
    before = len((await db_session.scalars(select(Shipment.id))).all())
    await seed_staging(db_session, SeedContext.create(30))
    assert len((await db_session.scalars(select(Shipment.id))).all()) == before


async def test_exporter_includes_shipments(db_session: AsyncSession) -> None:
    [export] = [fn for name, fn in exporters() if name.startswith("h3:")]
    tables = await export(db_session, SeedContext.create(30))
    rows = tables["shipments"]
    assert rows
    assert {"carrierShipmentId", "trackingUrls", "serviceRequestId", "labelFileId"} <= rows[
        0
    ].keys()
