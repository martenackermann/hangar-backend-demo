"""H4 test fixtures: a quiet fake booking provider on the test session and a ready stop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from procrastinate.testing import InMemoryConnector
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gateways
from app.core.enums import AssetStatus, ServiceRequestStatus, TourStatus, TourStopOutcome
from app.core.gateways.fakes.booking import FakeBookingProvider, shared_session
from app.core.gateways.protocols import CreateAppointmentRequest
from app.core.models import Asset, ServiceRequest, User
from app.core.timeutil import business_datetime, business_today
from app.modules.h4_field.models import Appointment, Tour, TourStop
from app.testing import factories as f

RIVERTON_CAL = 7  # fixed calendar ids: regions sorted by name (SPEC_RESOLUTIONS #6)
DANA = "dana.dispatcher@example.com"
THEO = "theo.technician@example.com"
TARA = "tara.lindqvist@example.com"
VIC = "vic.viewer@example.com"
ADA = "ada.admin@example.com"


class QuietFake(FakeBookingProvider):
    """Fake provider without the demo booking simulation (deterministic tests)."""

    async def simulate_bookings(self, session: AsyncSession, now: datetime) -> int:
        return 0


@pytest.fixture
def booking(db_session: AsyncSession) -> Iterator[QuietFake]:
    provider = QuietFake(shared_session(db_session))
    restore = gateways.override(booking=provider)
    try:
        yield provider
    finally:
        restore()


def deferred(jobs: InMemoryConnector, task: str = "notifications.send") -> list[dict[str, Any]]:
    return [j["args"] for j in jobs.jobs.values() if j["task_name"] == task]


@dataclass
class StopFixture:
    sr: ServiceRequest
    tour: Tour
    stop: TourStop
    new_asset: Asset
    old_asset: Asset
    appointment: Appointment


MakeStop = Callable[..., Awaitable[StopFixture]]


@pytest.fixture
def make_stop(db_session: AsyncSession, booking: QuietFake) -> MakeStop:
    """A provided SR (new laptop in transit + old device in use) on a tour of today."""

    async def _make(
        *,
        technician: str | None = THEO,
        tour_date: date | None = None,
        tour_status: TourStatus = TourStatus.PLANNED,
        sr_status: ServiceRequestStatus = ServiceRequestStatus.PROVIDED,
    ) -> StopFixture:
        day = tour_date or business_today()
        start = business_datetime(day, 9)
        old_asset = await f.make_asset(
            db_session, status=AssetStatus.IN_USE, location_code="IN_USE", place=None
        )
        sr = await f.make_sr(
            db_session,
            status=sr_status,
            appointment=start,
            old_tags=(old_asset.asset_tag or "",),
            appointment_calendar_id=RIVERTON_CAL,
            appointment_calendar_name="Riverton route",
        )
        new_asset = await f.make_asset(
            db_session,
            status=AssetStatus.PROVIDED,
            location_code="IN_TRANSIT",
            place=None,
            sr=sr,
        )
        item = sr.new_items[0]
        item.asset = new_asset
        item.asset_id = new_asset.id
        old_item = sr.old_items[0]
        old_item.asset = old_asset
        old_item.asset_id = old_asset.id
        booked = await booking.create_appointment(
            CreateAppointmentRequest(
                calendar_external_id="cal-riverton",
                service_id="svc-hw-rollout",
                start_at=start,
                end_at=start + timedelta(hours=1),
                reference=sr.number,
                contact_name=sr.contact.display_name,
                contact_email=sr.contact.email,
                street=None,
                town="Riverton",
                notes=None,
            )
        )
        appointment = Appointment(
            external_id=booked.external_id,
            calendar_id=RIVERTON_CAL,
            service_request_id=sr.id,
            reference=sr.number,
            status="booked",
            start_at=start,
            end_at=start + timedelta(hours=1),
            booked_at=booked.created_at,
            source="sync",
            is_current=True,
        )
        db_session.add(appointment)
        tech: User | None = await f.user(db_session, technician) if technician else None
        dana = await f.user(db_session, DANA)
        await _free_cell(db_session, RIVERTON_CAL, day)
        tour = Tour(
            calendar_id=RIVERTON_CAL,
            tour_date=day,
            technician_id=tech.id if tech else None,
            status=tour_status,
            created_by=dana.id,
            started_at=datetime.now().astimezone()
            if tour_status == TourStatus.IN_PROGRESS
            else None,
            stops=[],
        )
        stop = TourStop(
            service_request=sr,
            service_request_id=sr.id,
            position=1,
            outcome=TourStopOutcome.PENDING,
        )
        tour.stops.append(stop)
        db_session.add(tour)
        await db_session.flush()
        return StopFixture(sr, tour, stop, new_asset, old_asset, appointment)

    return _make


async def _free_cell(session: AsyncSession, calendar_id: int, day: date) -> None:
    """Cancel seeded tours in the same (calendar, day) so the fixture tour is the active one."""
    from sqlalchemy import select

    others = (
        await session.scalars(
            select(Tour).where(
                Tour.calendar_id == calendar_id,
                Tour.tour_date == day,
                Tour.status != TourStatus.CANCELLED,
            )
        )
    ).all()
    for other in others:
        for stop in [s for s in other.stops if s.outcome == TourStopOutcome.PENDING]:
            other.stops.remove(stop)
        other.status = TourStatus.CANCELLED
    await session.flush()
