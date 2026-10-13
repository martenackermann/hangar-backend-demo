"""Fake gateways (SPEC §8) and shared read models (SPEC_RESOLUTIONS #1)."""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from decimal import Decimal

import pytest
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ImportType, ServiceRequestStatus
from app.core.gateways.fakes.booking import FakeBookingProvider, shared_session
from app.core.gateways.fakes.carrier import FakeParcelCarrier
from app.core.gateways.fakes.ticketing import FakeTicketingImportSource
from app.core.gateways.protocols import (
    CarrierError,
    CreateAppointmentRequest,
    Parcel,
    PostalAddress,
    ShipmentRequest,
)
from app.core.models import LeaseReturn, ServiceRequest, User
from app.core.read_models import (
    build_contact_detail,
    build_lease_detail,
    build_sr_detail,
    build_sr_list_items,
)
from app.core.timeutil import now_utc

ADDRESS = PostalAddress(
    "Mia Turner", None, "18 Harbour Road", "50110", "Westbay", "Freeland", "mia.turner@example.com"
)
HUB = PostalAddress("Eastgate Hub", None, "88 Dock Road", "30030", "Eastgate", "Freeland", None)


async def test_fake_booking_provider_roundtrip(db_session: AsyncSession) -> None:
    provider = FakeBookingProvider(session_factory=shared_session(db_session))
    assert len(await provider.list_calendars()) == 9
    start = now_utc().replace(microsecond=0) + timedelta(days=3)
    created = await provider.create_appointment(
        CreateAppointmentRequest(
            "cal-westbay",
            "svc-hw-rollout",
            start,
            start + timedelta(hours=1),
            "SR0012999",
            "Mia Turner",
            "mia.turner@example.com",
            "18 Harbour Road",
            "Westbay",
            None,
        )
    )
    assert created.external_id.startswith("appt-")
    assert created.status == "booked"
    first = await provider.sync_appointments(None)
    assert any(c.appointment.external_id == created.external_id for c in first.changes)
    await provider.cancel_appointment(created.external_id, reason="test")
    second = await provider.sync_appointments(first.next_sync_token)
    kinds = {c.appointment.external_id: c.kind for c in second.changes}
    assert kinds[created.external_id] == "cancelled"
    listed = [
        a
        async for a in provider.list_appointments(
            "cal-westbay", since=start.date() - timedelta(days=1)
        )
    ]
    assert any(a.external_id == created.external_id for a in listed)
    link = provider.build_booking_link(
        destination_id="dest-westbay", service_id="svc-hw-rollout", reference="SR0012999"
    )
    assert link == "https://booking.example.com/book/dest-westbay/svc-hw-rollout?ref=SR0012999"


async def test_simulated_bookings_are_deterministic(db_session: AsyncSession) -> None:
    provider = FakeBookingProvider(session_factory=shared_session(db_session))
    when = now_utc().replace(minute=0, second=0, microsecond=0)
    first = await provider.simulate_bookings(db_session, when)
    assert first > 0
    second = await provider.simulate_bookings(db_session, when)
    assert second <= first  # already-booked SRs are skipped


async def test_fake_carrier(db_session: AsyncSession) -> None:
    carrier = FakeParcelCarrier(session_factory=shared_session(db_session))
    request = ShipmentRequest(
        "SR0012999",
        HUB,
        ADDRESS,
        [Parcel(Decimal("1.6"), ["GX200001", "SR0012999"])],
        [Parcel(Decimal("2.0"), ["GX100001"])],
        HUB,
    )
    result = await carrier.create_shipment(request)
    assert result.carrier_shipment_id.startswith("PC")
    assert len(result.carrier_shipment_id) == 16
    assert result.tracking_urls == [f"https://track.example.com/{result.parcel_ids[0]}"]
    assert result.return_tracking_urls[0].startswith("https://track.example.com/return/")
    pdf = PdfReader(io.BytesIO(result.label_pdf))
    assert len(pdf.pages) == 2
    assert "DEMO LABEL" in pdf.pages[0].extract_text()
    with pytest.raises(CarrierError, match="rejected"):
        await carrier.create_shipment(
            ShipmentRequest(
                "x",
                HUB,
                PostalAddress("A", None, "S", "00000", "C", "F", None),
                request.parcels,
                [],
                None,
            )
        )


@pytest.mark.parametrize("import_type", list(ImportType))
def test_fake_itsm_exports(import_type: ImportType) -> None:
    source = FakeTicketingImportSource()
    data = source.generate_export(import_type, rows=12, seed=7, with_errors=True)
    assert data == source.generate_export(
        import_type, rows=12, seed=7, with_errors=True
    )  # deterministic
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
    assert len(rows) == 12
    signature = {
        ImportType.SERVICE_REQUESTS: "order_details",
        ImportType.LEASE_RETURNS: "retirement_date",
        ImportType.ASSET_STATUS_SYNC: "install_state",
        ImportType.ASSETS_NEW: "serial_number",
        ImportType.CONTACTS: "last_name",
    }[import_type]
    assert signature in rows[0]


async def test_sr_detail_builder(db_session: AsyncSession) -> None:
    dana = (
        await db_session.scalars(select(User).where(User.email == "dana.dispatcher@example.com"))
    ).one()
    sr = (
        await db_session.scalars(
            select(ServiceRequest).where(
                ServiceRequest.status == ServiceRequestStatus.PERSONALIZATION_DONE
            )
        )
    ).first()
    assert sr is not None
    detail = await build_sr_detail(db_session, sr, dana)
    data = detail.model_dump(mode="json", by_alias=True)
    assert data["stage"] == "personalizing"
    assert data["items"][0]["direction"] in ("deliver", "collect")
    assert data["bookingLink"] is None or data["bookingLink"].startswith(
        "https://booking.example.com/book/"
    )
    assert data["milestones"]["personalizedAt"] is not None
    assert data["statusChangedAt"].endswith("Z")
    items = await build_sr_list_items(db_session, [sr], dana)
    assert items[0].id == sr.id


async def test_lease_and_contact_builders(db_session: AsyncSession) -> None:
    lease = (
        await db_session.scalars(
            select(LeaseReturn).where(LeaseReturn.reminder_1_sent_at.is_not(None))
        )
    ).first()
    assert lease is not None
    data = (await build_lease_detail(db_session, lease, None)).model_dump(
        mode="json", by_alias=True
    )
    assert "reminder1SentAt" in data
    assert data["asset"] is not None
    assert data["availableActions"] == []
    assert data["commentCount"] >= 0
    assert lease.contact is not None
    contact = (await build_contact_detail(db_session, lease.contact)).model_dump(by_alias=True)
    assert contact["openLeaseReturns"] >= 1
