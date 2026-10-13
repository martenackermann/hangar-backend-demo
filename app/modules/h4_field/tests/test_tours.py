"""Tours, tour planner, My tour and the seed story (SPEC §4.7, §6.12, §11.6)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ServiceRequestStatus, TourStatus, TourStopOutcome
from app.core.models import BookingSandboxAppointment, ServiceRequest, User
from app.core.notifications import render
from app.core.seed.framework import SeedContext
from app.core.timeutil import business_datetime, business_today, week_monday
from app.modules.h4_field.models import Appointment, BookingSyncState, Tour
from app.modules.h4_field.protocol import PROTOCOL_SAMPLE
from app.testing import factories as f

from .conftest import ADA, DANA, RIVERTON_CAL, TARA, THEO, VIC, MakeStop, deferred

SR = ServiceRequestStatus
Headers = Callable[[str], Awaitable[dict[str, str]]]


async def _candidate(
    db_session: AsyncSession, day: date, hour: int = 10, **kwargs: Any
) -> ServiceRequest:
    return await f.make_sr(
        db_session,
        status=kwargs.pop("status", SR.PROVIDED),
        appointment=business_datetime(day, hour),
        appointment_calendar_id=RIVERTON_CAL,
        appointment_calendar_name="Riverton route",
        **kwargs,
    )


def _free_day() -> date:
    """A weekday in three weeks: no seeded tour exists there."""
    day = week_monday(business_today()) + timedelta(days=21)
    return day


async def test_create_tour_assign_and_reorder(
    client: AsyncClient, headers: Headers, db_session: AsyncSession, jobs: InMemoryConnector
) -> None:
    day = _free_day()
    first = await _candidate(db_session, day, 9)
    second = await _candidate(db_session, day, 11, status=SR.APPOINTMENT_BOOKED)
    theo = await f.user(db_session, THEO)
    dana = await headers(DANA)
    body = {
        "calendarId": RIVERTON_CAL,
        "tourDate": day.isoformat(),
        "serviceRequestIds": [first.id],
    }
    viewer = await client.post("/api/v1/tours", json=body, headers=await headers(VIC))
    assert viewer.status_code == 403
    created = await client.post("/api/v1/tours", json=body, headers=dana)
    assert created.status_code == 201, created.text
    tour = created.json()
    assert tour["status"] == "draft"
    assert tour["technician"] is None
    assert [s["serviceRequest"]["number"] for s in tour["stops"]] == [first.number]
    assert set(tour["availableActions"]) == {"cancel", "assign_technician"}
    dup = await client.post("/api/v1/tours", json=body, headers=dana)
    assert dup.status_code == 409
    assert dup.json()["code"] == "tour_exists"
    assert str(tour["id"]) in dup.json()["detail"]

    url = f"/api/v1/tours/{tour['id']}"
    bad_tech = await client.patch(
        url, json={"technicianId": (await f.user(db_session, DANA)).id}, headers=dana
    )
    assert bad_tech.status_code == 422
    assigned = await client.patch(
        url, json={"technicianId": theo.id, "version": tour["version"]}, headers=dana
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["status"] == "planned"
    mails = [a for a in deferred(jobs) if a["template_id"] == "tour.assigned"]
    assert mails
    assert mails[-1]["to"] == [theo.email]
    stale = await client.patch(url, json={"notes": "x", "version": tour["version"]}, headers=dana)
    assert stale.status_code == 409
    assert stale.json()["code"] == "version_conflict"

    stops = await client.put(
        f"{url}/stops", json={"serviceRequestIds": [second.id, first.id]}, headers=dana
    )
    assert stops.status_code == 200, stops.text
    detail = stops.json()
    assert [s["serviceRequest"]["id"] for s in detail["stops"]] == [second.id, first.id]
    assert [s["position"] for s in detail["stops"]] == [1, 2]
    assert "stops_not_ready" in detail["warnings"]
    assert detail["readiness"] == {"ready": 1, "to_pick": 0, "in_staging": 0, "not_started": 1}
    outsider = await f.make_sr(db_session, status=SR.PROVIDED)
    conflict = await client.put(
        f"{url}/stops", json={"serviceRequestIds": [outsider.id]}, headers=dana
    )
    assert conflict.status_code == 409

    unassigned = await client.patch(url, json={"technicianId": None}, headers=dana)
    assert unassigned.json()["status"] == "draft"
    cancelled = await client.post(f"{url}/actions/cancel", json={}, headers=dana)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["stops"] == []


async def test_tour_start_rules(client: AsyncClient, headers: Headers, make_stop: MakeStop) -> None:
    today = await make_stop()
    later = await make_stop(tour_date=business_today() + timedelta(days=7))
    url = f"/api/v1/tours/{today.tour.id}/actions/start"
    tara = await client.post(url, json={}, headers=await headers(TARA))
    assert tara.status_code == 403
    assert tara.json()["code"] == "not_own_tour"
    vic = await client.post(url, json={}, headers=await headers(VIC))
    assert vic.status_code == 403
    wrong_day = await client.post(
        f"/api/v1/tours/{later.tour.id}/actions/start", json={}, headers=await headers(THEO)
    )
    assert wrong_day.status_code == 409
    assert wrong_day.json()["guard"]["code"] == "tour_date_today"
    started = await client.post(url, json={}, headers=await headers(THEO))
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "in_progress"
    assert started.json()["startedAt"]
    again = await client.post(url, json={}, headers=await headers(THEO))
    assert again.status_code == 409
    assert again.json()["code"] == "transition_not_allowed"
    finish = await client.post(
        f"/api/v1/tours/{today.tour.id}/actions/finish", json={}, headers=await headers(THEO)
    )
    assert finish.status_code == 409
    assert finish.json()["guard"]["code"] == "no_pending_stops"
    unknown = await client.post(
        f"/api/v1/tours/{today.tour.id}/actions/fly", json={}, headers=await headers(THEO)
    )
    assert unknown.status_code == 404


async def test_tour_list_and_filters(client: AsyncClient, headers: Headers) -> None:
    week = await client.get("/api/v1/tours?perPage=200", headers=await headers(VIC))
    assert week.status_code == 200
    assert week.json()["meta"]["total"] > 0
    mine = await client.get("/api/v1/tours?technicianId=me", headers=await headers(THEO))
    assert all(t["technician"]["email"] == THEO for t in mine.json()["data"])
    drafts = await client.get(
        f"/api/v1/tours?technicianId=none&from={week_monday(business_today()).isoformat()}"
        f"&to={(week_monday(business_today()) + timedelta(days=13)).isoformat()}&sort=calendar",
        headers=await headers(VIC),
    )
    assert drafts.status_code == 200
    assert all(t["status"] == "draft" for t in drafts.json()["data"])
    too_long = await client.get(
        "/api/v1/tours?from=2026-01-01&to=2026-03-01", headers=await headers(VIC)
    )
    assert too_long.status_code == 422
    bad = await client.get("/api/v1/tours?technicianId=someone", headers=await headers(VIC))
    assert bad.status_code == 422


async def test_candidates_and_overview(
    client: AsyncClient, headers: Headers, make_stop: MakeStop, db_session: AsyncSession
) -> None:
    fx = await make_stop()
    loose = await _candidate(db_session, fx.tour.tour_date, 15)
    response = await client.get(
        f"/api/v1/tour-planner/candidates?calendarId={RIVERTON_CAL}&date={fx.tour.tour_date}",
        headers=await headers(VIC),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tour"]["id"] == fx.tour.id
    by_id = {c["serviceRequest"]["id"]: c for c in body["candidates"]}
    assert by_id[fx.sr.id]["inTourId"] == fx.tour.id
    assert by_id[loose.id]["inTourId"] is None
    assert by_id[loose.id]["readiness"] == "ready"
    unknown = await client.get(
        f"/api/v1/tour-planner/candidates?calendarId=999&date={fx.tour.tour_date}",
        headers=await headers(VIC),
    )
    assert unknown.status_code == 422

    overview = await client.get("/api/v1/tour-planner/overview", headers=await headers(VIC))
    assert overview.status_code == 200
    data = overview.json()
    assert len(data["days"]) == 5
    assert len(data["rows"]) == 9
    assert {t["technician"]["email"] for t in data["technicians"]} >= {THEO, TARA}
    too_long = await client.get(
        "/api/v1/tour-planner/overview?from=2026-10-01&to=2026-10-31", headers=await headers(VIC)
    )
    assert too_long.status_code == 422


async def test_my_tours(client: AsyncClient, headers: Headers) -> None:
    d0 = SeedContext.create().d0
    response = await client.get(f"/api/v1/my/tours?date={d0}", headers=await headers(THEO))
    assert response.status_code == 200
    [tour] = response.json()["data"]
    assert tour["calendar"]["name"] == "Riverton route"
    assert tour["pendingCount"] == 5
    assert tour["status"] == "planned"
    times = [s["serviceRequest"]["appointmentStartAt"] for s in tour["stops"]]
    assert times == sorted(times)
    viewer = await client.get("/api/v1/my/tours", headers=await headers(VIC))
    assert viewer.status_code == 403


# --- seed story -------------------------------------------------------------------------------


async def test_seed_story(db_session: AsyncSession, user: Callable[[str], Awaitable[User]]) -> None:
    ctx = SeedContext.create()
    theo = await user(THEO)
    unmatched = await db_session.scalar(
        select(func.count()).where(Appointment.service_request_id.is_(None))
    )
    assert unmatched == 5
    with_appt = await db_session.scalar(
        select(func.count()).where(ServiceRequest.appointment_start_at.is_not(None))
    )
    current = await db_session.scalar(select(func.count()).where(Appointment.is_current.is_(True)))
    assert current == with_appt
    cancelled = await db_session.scalar(
        select(func.count()).where(Appointment.status == "cancelled")
    )
    superseded = await db_session.scalar(
        select(func.count()).where(Appointment.superseded_at.is_not(None))
    )
    assert (cancelled or 0) + (superseded or 0) == 8  # one per reschedule_needed SR

    today = (
        await db_session.scalars(
            select(Tour).where(Tour.technician_id == theo.id, Tour.tour_date == ctx.d0)
        )
    ).one()
    assert today.status == TourStatus.PLANNED
    assert [s.outcome for s in today.stops] == [TourStopOutcome.PENDING] * 5
    assert all(s.service_request.status == SR.PROVIDED for s in today.stops)
    yesterday = (
        await db_session.scalars(
            select(Tour).where(Tour.technician_id == theo.id, Tour.tour_date == ctx.wd(-1))
        )
    ).one()
    outcomes = sorted(str(s.outcome) for s in yesterday.stops)
    assert yesterday.status == TourStatus.COMPLETED
    assert outcomes == ["completed", "completed", "completed", "rescheduled"]

    missing_protocol = await db_session.scalar(
        select(func.count()).where(
            ServiceRequest.status == SR.COMPLETED, ServiceRequest.protocol_file_id.is_(None)
        )
    )
    assert missing_protocol == 0
    state = await db_session.get(BookingSyncState, 1)
    max_seq = await db_session.scalar(select(func.max(BookingSandboxAppointment.change_seq)))
    assert state is not None
    assert state.sync_token == str(max_seq)


def test_templates_render() -> None:
    protocol = render("sr.protocol", dict(PROTOCOL_SAMPLE))
    assert protocol.subject == "Handover protocol for SR0012345"
    assert "GX204811" in protocol.html
    assert "Dear Ms Turner" in protocol.text
    from app.core.notifications import templates

    assigned = render("tour.assigned", dict(templates.get("tour.assigned").sample_context))
    assert assigned.subject == "Tour Riverton route on 13 Oct 2026 assigned to you (2 stops)"
    assert "/field/my-tour?date=2026-10-13" in assigned.text
    assert "08:00" in assigned.text  # business time zone


async def test_admin_can_plan(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    day = _free_day() + timedelta(days=1)
    sr = await _candidate(db_session, day, 9)
    response = await client.post(
        "/api/v1/tours",
        json={
            "calendarId": RIVERTON_CAL,
            "tourDate": day.isoformat(),
            "technicianId": (await f.user(db_session, THEO)).id,
            "serviceRequestIds": [sr.id],
            "notes": "Bring spare dock",
        },
        headers=await headers(ADA),
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "planned"
    assert response.json()["notes"] == "Bring spare dock"
