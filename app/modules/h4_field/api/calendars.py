"""``/booking-calendars`` and ``/appointments`` (API.md §7.1)."""

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.enums import AppointmentStatus, JobKind
from app.core.errors import ConflictError, NotFoundError, ValidationProblem, problem_responses
from app.core.jobs import enqueue
from app.core.models import Region, ServiceRequest, User
from app.core.pagination import (
    OrderBy,
    Page,
    PageParamsDep,
    csv_enum,
    csv_ints,
    fetch_page,
    search_clause,
    sort_param,
)
from app.core.schemas import JobAccepted, ListResponse
from app.core.timeutil import business_datetime

from ..appointments import (
    build_appointments,
    build_calendar,
    counts_from,
    link_appointment,
    next_scheduled_at,
    sr_by_number,
    sync_running,
    upcoming_counts,
)
from ..models import Appointment, BookingCalendar, BookingSyncState
from ..schemas import (
    AppointmentLinkBody,
    AppointmentOut,
    BookingCalendarOut,
    BookingCalendarPatch,
    SyncStateOut,
)
from ..tasks import TASK_NAME

router = APIRouter(tags=["field"])

_READ = problem_responses(401, 403, 422)
_WRITE = problem_responses(401, 403, 404, 409, 422)

SORT = sort_param(
    {"startAt": Appointment.start_at, "bookedAt": Appointment.booked_at},
    default="startAt",
    tiebreaker=Appointment.id,
)


# --- calendars --------------------------------------------------------------------------------


@router.get(
    "/booking-calendars",
    response_model=ListResponse[BookingCalendarOut],
    summary="Booking calendars",
    responses=_READ,
)
async def list_calendars(
    session: SessionDep,
    _user: Annotated[User, Depends(require("appointments.read"))],
) -> ListResponse[BookingCalendarOut]:
    calendars = (
        await session.scalars(select(BookingCalendar).order_by(BookingCalendar.name))
    ).all()
    upcoming = await upcoming_counts(session)
    return ListResponse[BookingCalendarOut](
        data=[build_calendar(c, upcoming.get(c.id, 0)) for c in calendars]
    )


@router.patch(
    "/booking-calendars/{calendar_id}",
    response_model=BookingCalendarOut,
    summary="Edit a booking calendar",
    responses=_WRITE,
)
async def patch_calendar(
    calendar_id: int,
    body: BookingCalendarPatch,
    session: SessionDep,
    _user: Annotated[User, Depends(require("master.manage"))],
) -> BookingCalendarOut:
    calendar = await session.get(BookingCalendar, calendar_id)
    if calendar is None:
        raise NotFoundError(f"Booking calendar {calendar_id} not found.")
    fields = body.model_fields_set
    if "name" in fields:
        if body.name is None or not body.name.strip():
            raise ValidationProblem({"name": ["A name is required."]})
        calendar.name = body.name.strip()
    if "region_id" in fields:
        if body.region_id is not None:
            region = await session.get(Region, body.region_id)
            if region is None:
                raise ValidationProblem({"regionId": ["Unknown region."]})
            calendar.region = region
        else:
            calendar.region = None
        calendar.region_id = body.region_id
    if "is_enabled" in fields:
        if body.is_enabled is None:
            raise ValidationProblem({"isEnabled": ["Choose enabled or disabled."]})
        calendar.is_enabled = body.is_enabled
    await session.flush()
    await commit(session)
    upcoming = await upcoming_counts(session)
    return build_calendar(calendar, upcoming.get(calendar.id, 0))


# --- appointments -----------------------------------------------------------------------------


@router.get(
    "/appointments",
    response_model=Page[AppointmentOut],
    summary="Appointments",
    responses=_READ,
)
async def list_appointments(
    session: SessionDep,
    _user: Annotated[User, Depends(require("appointments.read"))],
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
    calendar_ids: Annotated[list[int | str] | None, Depends(csv_ints("calendarId"))] = None,
    statuses: Annotated[
        list[AppointmentStatus] | None, Depends(csv_enum("status", AppointmentStatus))
    ] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    service_request_id: Annotated[int | None, Query(alias="serviceRequestId")] = None,
    unmatched: Annotated[bool | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[AppointmentOut]:
    stmt = select(Appointment)
    if calendar_ids:
        stmt = stmt.where(Appointment.calendar_id.in_([int(i) for i in calendar_ids]))
    if statuses:
        stmt = stmt.where(Appointment.status.in_(statuses))
    if date_from is not None:
        stmt = stmt.where(Appointment.start_at >= business_datetime(date_from))
    if date_to is not None:
        stmt = stmt.where(Appointment.start_at < business_datetime(date_to + timedelta(days=1)))
    if service_request_id is not None:
        stmt = stmt.where(Appointment.service_request_id == service_request_id)
    if unmatched is True:
        stmt = stmt.where(Appointment.service_request_id.is_(None))
    elif unmatched is False:
        stmt = stmt.where(Appointment.service_request_id.is_not(None))
    clause = search_clause(
        q,
        Appointment.reference,
        Appointment.title,
        Appointment.contact_name,
        Appointment.contact_email,
        Appointment.town,
    )
    if clause is not None:
        stmt = stmt.where(clause)
    rows, meta = await fetch_page(session, stmt.order_by(*order), paging)
    return Page[AppointmentOut](data=await build_appointments(session, rows), meta=meta)


@router.post(
    "/appointments/sync",
    status_code=202,
    response_model=JobAccepted,
    summary="Run the appointment sync now",
    responses=problem_responses(401, 403, 409),
)
async def sync_now(
    session: SessionDep,
    user: Annotated[User, Depends(require("appointments.manage"))],
) -> JobAccepted:
    running = await sync_running(session)
    if running is not None:
        raise ConflictError(
            f"An appointment sync is already running (job {running.id}).", jobId=running.id
        )
    job = await enqueue(session, TASK_NAME, kind=JobKind.APPOINTMENTS_SYNC, created_by=user)
    await commit(session)
    return JobAccepted(job_id=job.id)


@router.get(
    "/appointments/sync-state",
    response_model=SyncStateOut,
    summary="Appointment sync state",
    responses=_READ,
)
async def sync_state(
    session: SessionDep,
    _user: Annotated[User, Depends(require("appointments.read"))],
) -> SyncStateOut:
    state = await session.get(BookingSyncState, 1)
    running = await sync_running(session)
    return SyncStateOut(
        running=running is not None,
        last_job_id=running.id if running is not None else (state.last_job_id if state else None),
        last_incremental_sync_at=state.last_incremental_sync_at if state else None,
        last_full_sync_at=state.last_full_sync_at if state else None,
        last_result=counts_from(state.last_result) if state else None,
        next_scheduled_at=next_scheduled_at(),
    )


@router.post(
    "/appointments/{appointment_id}/link",
    response_model=AppointmentOut,
    summary="Link an unmatched appointment to a service request",
    responses=_WRITE,
)
async def link(
    appointment_id: int,
    body: AppointmentLinkBody,
    session: SessionDep,
    user: Annotated[User, Depends(require("appointments.manage"))],
) -> AppointmentOut:
    row = await session.get(Appointment, appointment_id, with_for_update=True)
    if row is None:
        raise NotFoundError(f"Appointment {appointment_id} not found.")
    if row.service_request_id is not None:
        raise ConflictError("This appointment is already linked to a service request.")
    sr = await sr_by_number(session, body.service_request_number)
    if sr is None:
        raise NotFoundError(f"Service request {body.service_request_number} not found.")
    await link_appointment(session, row, sr, user)
    await commit(session)
    [out] = await build_appointments(session, [row])
    return out


@router.get(
    "/service-requests/{sr_id}/appointments",
    response_model=ListResponse[AppointmentOut],
    summary="Appointments of a service request",
    responses=problem_responses(401, 403, 404),
)
async def sr_appointments(
    sr_id: int,
    session: SessionDep,
    _user: Annotated[User, Depends(require("sr.read"))],
) -> ListResponse[AppointmentOut]:
    if await session.get(ServiceRequest, sr_id) is None:
        raise NotFoundError(f"Service request {sr_id} not found.")
    rows = (
        await session.scalars(
            select(Appointment)
            .where(Appointment.service_request_id == sr_id)
            .order_by(
                Appointment.booked_at.desc().nulls_last(),
                Appointment.start_at.desc(),
                Appointment.id.desc(),
            )
        )
    ).all()
    return ListResponse[AppointmentOut](data=await build_appointments(session, rows))
