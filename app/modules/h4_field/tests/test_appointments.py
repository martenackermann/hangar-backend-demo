"""Calendars, appointments, book / reschedule actions and the provider sync (SPEC §6.18)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import NoReturn

from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gateways
from app.core.enums import AssetStatus, ServiceRequestStatus, TourStatus, TourStopOutcome
from app.core.gateways.protocols import BookingProviderError, CreateAppointmentRequest
from app.core.models import BookingSandboxAppointment, ServiceRequest, User
from app.core.timeutil import business_datetime, business_today, now_utc
from app.modules.h4_field.appointments import next_scheduled_at, run_sync
from app.modules.h4_field.models import Appointment, BookingCalendar, BookingSyncState
from app.modules.h4_field.tasks import TASK_NAME
from app.testing import factories as f

from .conftest import ADA, DANA, RIVERTON_CAL, THEO, VIC, MakeStop, QuietFake, deferred

SR = ServiceRequestStatus
Headers = Callable[[str], Awaitable[dict[str, str]]]


async def _next_seq(session: AsyncSession) -> int:
    value = await session.scalar(select(func.nextval("booking_sandbox_change_seq")))
    assert value is not None
    return int(value)


def _future(days: int = 5, hour: int = 10) -> datetime:
    from app.core.timeutil import add_working_days

    return business_datetime(add_working_days(business_today(), days), hour)


async def _book_at_provider(provider: QuietFake, sr: ServiceRequest, when: datetime) -> str:
    booked = await provider.create_appointment(
        CreateAppointmentRequest(
            calendar_external_id="cal-riverton",
            service_id="svc-hw-rollout",
            start_at=when,
            end_at=when + timedelta(hours=1),
            reference=sr.number,
            contact_name="Test Person",
            contact_email=None,
            street=None,
            town="Riverton",
            notes=None,
        )
    )
    return booked.external_id


# --- calendars --------------------------------------------------------------------------------


async def test_calendars_list_and_patch(client: AsyncClient, headers: Headers) -> None:
    listing = await client.get("/api/v1/booking-calendars", headers=await headers(VIC))
    assert listing.status_code == 200
    data = listing.json()["data"]
    assert [c["id"] for c in sorted(data, key=lambda c: c["id"])] == list(range(1, 10))
    riverton = next(c for c in data if c["id"] == RIVERTON_CAL)
    assert riverton["externalId"] == "cal-riverton"
    assert riverton["region"]["code"] == "riverton"
    assert riverton["upcomingAppointments"] > 0

    url = f"/api/v1/booking-calendars/{RIVERTON_CAL}"
    forbidden = await client.patch(url, json={"isEnabled": False}, headers=await headers(DANA))
    assert forbidden.status_code == 403
    bad = await client.patch(url, json={"regionId": 99999}, headers=await headers(ADA))
    assert bad.status_code == 422
    assert "regionId" in bad.json()["errors"]
    unknown_field = await client.patch(url, json={"color": "red"}, headers=await headers(ADA))
    assert unknown_field.status_code == 422
    ok = await client.patch(
        url, json={"isEnabled": False, "name": "Riverton north"}, headers=await headers(ADA)
    )
    assert ok.status_code == 200
    assert ok.json()["isEnabled"] is False
    assert ok.json()["name"] == "Riverton north"
    assert (await client.get("/api/v1/booking-calendars")).status_code == 401


async def test_appointments_list_filters(client: AsyncClient, headers: Headers) -> None:
    unmatched = await client.get(
        "/api/v1/appointments?unmatched=true&perPage=50", headers=await headers(VIC)
    )
    assert unmatched.status_code == 200
    assert unmatched.json()["meta"]["total"] == 5
    assert all(a["serviceRequest"] is None for a in unmatched.json()["data"])
    by_cal = await client.get(
        f"/api/v1/appointments?calendarId={RIVERTON_CAL}&status=booked&sort=-bookedAt",
        headers=await headers(VIC),
    )
    assert by_cal.status_code == 200
    assert all(a["calendar"]["id"] == RIVERTON_CAL for a in by_cal.json()["data"])
    bad = await client.get("/api/v1/appointments?status=maybe", headers=await headers(VIC))
    assert bad.status_code == 422
    bad_sort = await client.get("/api/v1/appointments?sort=title", headers=await headers(VIC))
    assert bad_sort.status_code == 422


async def test_sr_appointments_and_tour_stops(
    client: AsyncClient, headers: Headers, make_stop: MakeStop
) -> None:
    fx = await make_stop()
    appts = await client.get(
        f"/api/v1/service-requests/{fx.sr.id}/appointments", headers=await headers(VIC)
    )
    assert appts.status_code == 200
    [appt] = appts.json()["data"]
    assert appt["isCurrent"] is True
    assert appt["serviceRequest"]["number"] == fx.sr.number
    stops = await client.get(
        f"/api/v1/service-requests/{fx.sr.id}/tour-stops", headers=await headers(VIC)
    )
    assert stops.status_code == 200
    [ref] = stops.json()["data"]
    assert ref == {
        "tourId": fx.tour.id,
        "stopId": fx.stop.id,
        "tourDate": fx.tour.tour_date.isoformat(),
        "technicianName": "Theo Technician",
        "status": "planned",
        "outcome": "pending",
    }


# --- link + sync ------------------------------------------------------------------------------


async def test_link_unmatched_appointment(
    client: AsyncClient, headers: Headers, db_session: AsyncSession, booking: QuietFake
) -> None:
    sr = await f.make_sr(db_session, status=SR.INVITED)
    row = (
        await db_session.scalars(
            select(Appointment)
            .where(Appointment.service_request_id.is_(None))
            .order_by(Appointment.start_at)
        )
    ).first()
    assert row is not None
    url = f"/api/v1/appointments/{row.id}/link"
    viewer = await client.post(
        url, json={"serviceRequestNumber": sr.number}, headers=await headers(VIC)
    )
    assert viewer.status_code == 403
    unknown = await client.post(
        url, json={"serviceRequestNumber": "SR9999999"}, headers=await headers(DANA)
    )
    assert unknown.status_code == 404
    response = await client.post(
        url, json={"serviceRequestNumber": sr.number.lower()}, headers=await headers(DANA)
    )
    assert response.status_code == 200, response.text
    assert response.json()["serviceRequest"]["status"] == "appointment_booked"
    await db_session.refresh(sr)
    assert sr.appointment_start_at == row.start_at
    assert sr.appointment_calendar_id == row.calendar_id
    again = await client.post(
        url, json={"serviceRequestNumber": sr.number}, headers=await headers(DANA)
    )
    assert again.status_code == 409


async def test_sync_books_moves_and_cancels(
    db_session: AsyncSession, booking: QuietFake, make_stop: MakeStop
) -> None:
    invited = await f.make_sr(db_session, status=SR.INVITED)
    booked_sr = await f.make_sr(db_session, status=SR.INVITED)
    first = await _book_at_provider(booking, invited, _future(4))
    second = await _book_at_provider(booking, booked_sr, _future(6))
    counts = await run_sync(db_session, booking)
    assert counts.matched >= 2
    assert counts.transitions >= 2
    await db_session.refresh(invited)
    await db_session.refresh(booked_sr)
    assert invited.status == SR.APPOINTMENT_BOOKED
    assert invited.appointment_calendar_id == RIVERTON_CAL
    assert invited.appointment_calendar_name == "Riverton route"
    assert booked_sr.status == SR.APPOINTMENT_BOOKED

    # the user moves the appointment in the portal → appointment_* follows
    sandbox = (
        await db_session.scalars(
            select(BookingSandboxAppointment).where(BookingSandboxAppointment.external_id == first)
        )
    ).one()
    moved = _future(8, 14)
    sandbox.start_at, sandbox.end_at = moved, moved + timedelta(hours=1)
    sandbox.change_seq = await _next_seq(db_session)
    await db_session.flush()
    await run_sync(db_session, booking)
    await db_session.refresh(invited)
    assert invited.appointment_start_at == moved
    assert invited.status == SR.APPOINTMENT_BOOKED

    # the user cancels → appointment_cancelled → reschedule_needed (resume = booked)
    await booking.cancel_appointment(second, reason="test")
    counts = await run_sync(db_session, booking)
    assert counts.cancelled == 1
    await db_session.refresh(booked_sr)
    assert booked_sr.status == SR.RESCHEDULE_NEEDED
    assert booked_sr.resume_status == SR.APPOINTMENT_BOOKED
    assert booked_sr.appointment_start_at is None

    # a new booking brings it back to its resume status
    await _book_at_provider(booking, booked_sr, _future(9))
    await run_sync(db_session, booking)
    await db_session.refresh(booked_sr)
    assert booked_sr.status == SR.APPOINTMENT_BOOKED
    assert booked_sr.resume_status is None


async def test_sync_cancel_in_progress_tour_keeps_stop(
    db_session: AsyncSession, booking: QuietFake, make_stop: MakeStop
) -> None:
    fx = await make_stop(tour_status=TourStatus.IN_PROGRESS)
    await booking.cancel_appointment(fx.appointment.external_id)
    await run_sync(db_session, booking)
    await db_session.refresh(fx.sr)
    await db_session.refresh(fx.stop)
    await db_session.refresh(fx.new_asset)
    assert fx.sr.status == SR.RESCHEDULE_NEEDED
    assert fx.sr.resume_status == SR.READY_FOR_PICKING
    assert fx.new_asset.status == AssetStatus.PREPARED  # return_to_hub
    assert fx.stop.outcome == TourStopOutcome.RESCHEDULED


async def test_sync_ignores_superseded_and_past(
    db_session: AsyncSession, booking: QuietFake
) -> None:
    sr = await f.make_sr(
        db_session, status=SR.RESCHEDULE_NEEDED, resume_status=SR.APPOINTMENT_BOOKED
    )
    external = await _book_at_provider(booking, sr, _future(3))
    await run_sync(db_session, booking)
    await db_session.refresh(sr)
    assert sr.status == SR.APPOINTMENT_BOOKED  # sanity: a fresh booking is applied
    # supersede it locally (on-site reschedule left it at the provider) and reschedule again
    row = (
        await db_session.scalars(select(Appointment).where(Appointment.external_id == external))
    ).one()
    row.is_current, row.superseded_at = False, now_utc()
    sr.status = SR.RESCHEDULE_NEEDED
    await db_session.flush()
    sandbox = (
        await db_session.scalars(
            select(BookingSandboxAppointment).where(
                BookingSandboxAppointment.external_id == external
            )
        )
    ).one()
    sandbox.notes = "touched"
    sandbox.change_seq = await _next_seq(db_session)
    await db_session.flush()
    counts = await run_sync(db_session, booking)
    await db_session.refresh(sr)
    assert sr.status == SR.RESCHEDULE_NEEDED
    assert counts.transitions == 0


async def test_full_sync_without_token(db_session: AsyncSession, booking: QuietFake) -> None:
    state = await db_session.get(BookingSyncState, 1)
    assert state is not None
    state.sync_token = None
    await db_session.flush()
    counts = await run_sync(db_session, booking)
    assert counts.calendars == 9
    await db_session.refresh(state)
    assert state.sync_token is not None
    assert state.last_full_sync_at is not None
    assert state.last_result is not None
    assert state.last_result["calendars"] == 9


async def test_sync_endpoint_and_state(
    client: AsyncClient, headers: Headers, jobs: InMemoryConnector
) -> None:
    viewer = await client.post("/api/v1/appointments/sync", json={}, headers=await headers(VIC))
    assert viewer.status_code == 403
    theo = await client.post("/api/v1/appointments/sync", json={}, headers=await headers(THEO))
    assert theo.status_code == 403
    response = await client.post("/api/v1/appointments/sync", json={}, headers=await headers(DANA))
    assert response.status_code == 202
    job_id = response.json()["jobId"]
    [args] = deferred(jobs, TASK_NAME)
    assert args["job_id"] == job_id
    again = await client.post("/api/v1/appointments/sync", json={}, headers=await headers(DANA))
    assert again.status_code == 409
    state = await client.get("/api/v1/appointments/sync-state", headers=await headers(VIC))
    assert state.status_code == 200
    body = state.json()
    assert body["running"] is True
    assert body["lastJobId"] == job_id
    assert body["lastResult"]["calendars"] == 9
    assert body["nextScheduledAt"].endswith(":05:00Z")


def test_next_scheduled_at() -> None:
    from datetime import UTC, datetime

    assert next_scheduled_at(datetime(2026, 10, 13, 7, 4, tzinfo=UTC)).minute == 5
    assert next_scheduled_at(datetime(2026, 10, 13, 7, 6, tzinfo=UTC)).hour == 8


# --- book_appointment / request_reschedule ----------------------------------------------------


async def test_book_appointment(
    client: AsyncClient, headers: Headers, db_session: AsyncSession, booking: QuietFake
) -> None:
    sr = await f.make_sr(db_session, status=SR.CAMPUS_INFO_SENT, campus=True)
    url = f"/api/v1/service-requests/{sr.id}/actions/book_appointment"
    start = _future(3)
    theo = await client.post(
        url,
        json={"calendarId": RIVERTON_CAL, "startAt": start.isoformat()},
        headers=await headers(THEO),
    )
    assert theo.status_code == 403
    past = await client.post(
        url,
        json={"calendarId": RIVERTON_CAL, "startAt": (now_utc() - timedelta(hours=2)).isoformat()},
        headers=await headers(DANA),
    )
    assert past.status_code == 409
    assert past.json()["guard"]["code"] == "start_in_future"
    unknown = await client.post(
        url, json={"calendarId": 999, "startAt": start.isoformat()}, headers=await headers(DANA)
    )
    assert unknown.status_code == 422
    assert "calendarId" in unknown.json()["errors"]
    calendar = await db_session.get(BookingCalendar, 2)
    assert calendar is not None
    calendar.is_enabled = False
    await db_session.flush()
    disabled = await client.post(
        url, json={"calendarId": 2, "startAt": start.isoformat()}, headers=await headers(DANA)
    )
    assert disabled.status_code == 409
    assert disabled.json()["guard"]["code"] == "calendar_enabled"

    response = await client.post(
        url,
        json={"calendarId": RIVERTON_CAL, "startAt": start.isoformat()},
        headers=await headers(DANA),
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["status"] == "appointment_booked"
    assert detail["appointmentCalendarId"] == RIVERTON_CAL
    row = (
        await db_session.scalars(select(Appointment).where(Appointment.service_request_id == sr.id))
    ).one()
    assert row.source == "manual"
    assert row.is_current is True
    assert row.end_at - row.start_at == timedelta(minutes=60)
    sandbox = await db_session.scalar(
        select(func.count()).where(BookingSandboxAppointment.external_id == row.external_id)
    )
    assert sandbox == 1


async def test_book_appointment_provider_failure(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    class Failing(QuietFake):
        async def create_appointment(self, request: CreateAppointmentRequest) -> NoReturn:
            raise BookingProviderError("Calendar is full")

    restore = gateways.override(booking=Failing())
    try:
        sr = await f.make_sr(db_session, status=SR.INVITED)
        response = await client.post(
            f"/api/v1/service-requests/{sr.id}/actions/book_appointment",
            json={"calendarId": RIVERTON_CAL, "startAt": _future(2).isoformat()},
            headers=await headers(DANA),
        )
    finally:
        restore()
    assert response.status_code == 502
    assert response.json()["code"] == "booking_error"
    rows = await db_session.scalar(
        select(func.count()).where(Appointment.service_request_id == sr.id)
    )
    assert rows == 0  # the request transaction is rolled back by the session (not in tests)


async def test_book_from_reschedule_resumes(
    client: AsyncClient, headers: Headers, db_session: AsyncSession, booking: QuietFake
) -> None:
    sr = await f.make_sr(
        db_session, status=SR.RESCHEDULE_NEEDED, resume_status=SR.PERSONALIZATION_DONE
    )
    response = await client.post(
        f"/api/v1/service-requests/{sr.id}/actions/book_appointment",
        json={"calendarId": RIVERTON_CAL, "startAt": _future(4).isoformat()},
        headers=await headers(DANA),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "personalization_done"
    assert response.json()["resumeStatus"] is None


async def test_request_reschedule(
    client: AsyncClient, headers: Headers, make_stop: MakeStop, db_session: AsyncSession
) -> None:
    fx = await make_stop()
    url = f"/api/v1/service-requests/{fx.sr.id}/actions/request_reschedule"
    missing = await client.post(url, json={"reason": "  "}, headers=await headers(DANA))
    assert missing.status_code == 422
    assert "reason" in missing.json()["errors"]
    theo = await client.post(url, json={"reason": "x"}, headers=await headers(THEO))
    assert theo.status_code == 403
    response = await client.post(
        url, json={"reason": "User is ill", "notifyUser": False}, headers=await headers(DANA)
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["status"] == "reschedule_needed"
    assert detail["resumeStatus"] == "ready_for_picking"
    await db_session.refresh(fx.appointment)
    await db_session.refresh(fx.new_asset)
    await db_session.refresh(fx.tour)
    assert fx.appointment.status == "cancelled"
    assert fx.appointment.is_current is False
    assert fx.new_asset.status == AssetStatus.PREPARED
    assert fx.tour.stops == []  # planned tour: the pending stop is deleted (#8)


async def test_request_reschedule_keep_provider_booking(
    client: AsyncClient, headers: Headers, make_stop: MakeStop, db_session: AsyncSession
) -> None:
    fx = await make_stop(sr_status=SR.SHIPPED)
    response = await client.post(
        f"/api/v1/service-requests/{fx.sr.id}/actions/request_reschedule",
        json={"reason": "Parcel delayed", "cancelAtProvider": False},
        headers=await headers(DANA),
    )
    assert response.status_code == 200, response.text
    assert response.json()["resumeStatus"] == "shipped"
    await db_session.refresh(fx.appointment)
    assert fx.appointment.status == "booked"
    assert fx.appointment.superseded_at is not None


async def test_cancel_subscriber_releases_stop(
    db_session: AsyncSession, make_stop: MakeStop, user: Callable[[str], Awaitable[User]]
) -> None:
    from app.core.enums import Trigger
    from app.core.transitions import transition_service

    planned = await make_stop()
    running = await make_stop(
        tour_status=TourStatus.IN_PROGRESS, tour_date=business_today() + timedelta(days=9)
    )
    dana = await user(DANA)
    for fx in (planned, running):
        await transition_service.apply(
            db_session,
            fx.sr,
            "cancel",
            actor=dana,
            trigger=Trigger.USER,
            comment="test",
        )
    await db_session.refresh(planned.tour)
    await db_session.refresh(running.stop)
    assert planned.tour.stops == []
    assert running.stop.outcome == TourStopOutcome.CANCELLED
