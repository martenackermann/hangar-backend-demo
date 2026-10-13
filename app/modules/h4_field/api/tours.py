"""``/tours``, ``/tour-planner/*``, ``/my/tours``, ``/service-requests/{id}/tour-stops``
(API.md §7.2, SPEC_RESOLUTIONS #21)."""

from collections import defaultdict
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.enums import Role, TourStatus
from app.core.errors import NotFoundError, ValidationProblem, problem_responses
from app.core.models import ServiceRequest, User
from app.core.pagination import (
    OrderBy,
    Page,
    PageParamsDep,
    csv_enum,
    csv_ints,
    fetch_page,
    sort_param,
)
from app.core.read_models import user_ref
from app.core.schemas import ListResponse
from app.core.timeutil import business_datetime, business_today, to_business_date, week_monday

from ..models import Appointment, BookingCalendar, Tour, TourStop
from ..schemas import (
    OverviewCell,
    OverviewRow,
    OverviewTour,
    TechnicianDay,
    TechnicianLoad,
    TourActionBody,
    TourCandidatesOut,
    TourCreate,
    TourDetailOut,
    TourPatch,
    TourPlannerOverviewOut,
    TourStopRef,
    TourStopsPut,
    TourSummaryOut,
)
from ..tours import (
    build_candidates,
    build_tour_detail,
    build_tour_summary,
    calendar_out,
    calendars_for,
    change_technician,
    check_tour_version,
    create_tour,
    load_calendar,
    load_tour,
    run_tour_action,
    set_stops,
    tour_warnings,
    tours_on,
    working_days,
)

router = APIRouter(tags=["field"])

_READ = problem_responses(401, 403, 404, 422)
_WRITE = problem_responses(401, 403, 404, 409, 422)
MAX_LIST_DAYS = 31
MAX_OVERVIEW_DAYS = 14

SORT = sort_param(
    {"tourDate": Tour.tour_date, "calendar": BookingCalendar.name},
    default="tourDate",
    tiebreaker=Tour.id,
)


def _range(
    date_from: date | None, date_to: date | None, *, max_days: int, default_days: int
) -> tuple[date, date]:
    start = date_from or week_monday(business_today())
    end = date_to or start + timedelta(days=default_days - 1)
    if end < start:
        raise ValidationProblem({"to": ["'to' must not be before 'from'."]})
    if (end - start).days + 1 > max_days:
        raise ValidationProblem({"to": [f"The range may span at most {max_days} days."]})
    return start, end


@router.get("/tours", response_model=Page[TourSummaryOut], summary="Tours", responses=_READ)
async def list_tours(
    session: SessionDep,
    user: Annotated[User, Depends(require("tours.read"))],
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    calendar_ids: Annotated[list[int | str] | None, Depends(csv_ints("calendarId"))] = None,
    region_ids: Annotated[list[int | str] | None, Depends(csv_ints("regionId"))] = None,
    technician_id: Annotated[
        str | None, Query(alias="technicianId", description="me | none | user id")
    ] = None,
    statuses: Annotated[list[TourStatus] | None, Depends(csv_enum("status", TourStatus))] = None,
) -> Page[TourSummaryOut]:
    start, end = _range(date_from, date_to, max_days=MAX_LIST_DAYS, default_days=7)
    stmt = (
        select(Tour)
        .join(BookingCalendar, BookingCalendar.id == Tour.calendar_id)
        .where(Tour.tour_date >= start, Tour.tour_date <= end)
    )
    if calendar_ids:
        stmt = stmt.where(Tour.calendar_id.in_([int(i) for i in calendar_ids]))
    if region_ids:
        stmt = stmt.where(BookingCalendar.region_id.in_([int(i) for i in region_ids]))
    if technician_id:
        if technician_id == "me":
            stmt = stmt.where(Tour.technician_id == user.id)
        elif technician_id == "none":
            stmt = stmt.where(Tour.technician_id.is_(None))
        elif technician_id.isdigit():
            stmt = stmt.where(Tour.technician_id == int(technician_id))
        else:
            raise ValidationProblem({"technicianId": ["Use 'me', 'none' or a user id."]})
    if statuses:
        stmt = stmt.where(Tour.status.in_(statuses))
    rows, meta = await fetch_page(session, stmt.order_by(*order), paging)
    return Page[TourSummaryOut](data=[build_tour_summary(t, user) for t in rows], meta=meta)


@router.post(
    "/tours",
    status_code=201,
    response_model=TourDetailOut,
    summary="Create a tour",
    responses=_WRITE,
)
async def post_tour(
    body: TourCreate,
    session: SessionDep,
    user: Annotated[User, Depends(require("tours.plan"))],
) -> TourDetailOut:
    tour = await create_tour(
        session,
        calendar_id=body.calendar_id,
        tour_date=body.tour_date,
        technician_id=body.technician_id,
        service_request_ids=body.service_request_ids,
        notes=body.notes,
        actor=user,
    )
    warnings = await tour_warnings(session, tour)
    await commit(session)
    return await build_tour_detail(session, tour, user, warnings)


@router.get("/tours/{tour_id}", response_model=TourDetailOut, summary="Tour", responses=_READ)
async def get_tour(
    tour_id: int,
    session: SessionDep,
    user: Annotated[User, Depends(require("tours.read"))],
) -> TourDetailOut:
    return await build_tour_detail(session, await load_tour(session, tour_id), user)


@router.patch(
    "/tours/{tour_id}", response_model=TourDetailOut, summary="Edit a tour", responses=_WRITE
)
async def patch_tour(
    tour_id: int,
    body: TourPatch,
    session: SessionDep,
    user: Annotated[User, Depends(require("tours.plan"))],
) -> TourDetailOut:
    tour = await load_tour(session, tour_id, for_update=True)
    check_tour_version(tour, body.version)
    fields = body.model_fields_set
    if "technician_id" in fields:
        await change_technician(session, tour, body.technician_id, user)
    if "notes" in fields:
        tour.notes = (body.notes or "").strip() or None
    await session.flush()
    warnings = await tour_warnings(session, tour)
    await commit(session)
    return await build_tour_detail(session, tour, user, warnings)


@router.put(
    "/tours/{tour_id}/stops",
    response_model=TourDetailOut,
    summary="Replace the ordered pending stops of a tour",
    responses=_WRITE,
)
async def put_stops(
    tour_id: int,
    body: TourStopsPut,
    session: SessionDep,
    user: Annotated[User, Depends(require("tours.plan"))],
) -> TourDetailOut:
    tour = await load_tour(session, tour_id, for_update=True)
    check_tour_version(tour, body.version)
    await set_stops(session, tour, body.service_request_ids, user)
    warnings = await tour_warnings(session, tour)
    await commit(session)
    return await build_tour_detail(session, tour, user, warnings)


@router.post(
    "/tours/{tour_id}/actions/{action}",
    response_model=TourDetailOut,
    summary="Run a tour action (start, finish, cancel)",
    responses=_WRITE,
)
async def tour_action(
    tour_id: int,
    action: str,
    session: SessionDep,
    user: Annotated[User, Depends(require("tours.read"))],
    body: TourActionBody | None = None,
) -> TourDetailOut:
    tour = await load_tour(session, tour_id, for_update=True)
    if body is not None:
        check_tour_version(tour, body.version)
    await run_tour_action(session, tour, action, user)
    await commit(session)
    return await build_tour_detail(session, tour, user)


# --- planner ----------------------------------------------------------------------------------


@router.get(
    "/tour-planner/candidates",
    response_model=TourCandidatesOut,
    summary="Tour candidates for a route and day",
    responses=_READ,
)
async def candidates(
    session: SessionDep,
    user: Annotated[User, Depends(require("tours.read"))],
    calendar_id: Annotated[int, Query(alias="calendarId")],
    day: Annotated[date, Query(alias="date")],
) -> TourCandidatesOut:
    calendar = await load_calendar(session, calendar_id)
    return await build_candidates(session, calendar, day, user)


@router.get(
    "/tour-planner/overview",
    response_model=TourPlannerOverviewOut,
    summary="Week grid of routes, appointments and tours",
    responses=_READ,
)
async def overview(
    session: SessionDep,
    _user: Annotated[User, Depends(require("tours.read"))],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    region_ids: Annotated[list[int | str] | None, Depends(csv_ints("regionId"))] = None,
) -> TourPlannerOverviewOut:
    start, end = _range(date_from, date_to, max_days=MAX_OVERVIEW_DAYS, default_days=5)
    days = working_days(start, end)
    calendars = await calendars_for(session, [int(i) for i in region_ids] if region_ids else None)
    calendar_ids = [c.id for c in calendars]

    counts: dict[tuple[int, date], int] = defaultdict(int)
    rows = await session.execute(
        select(Appointment.calendar_id, Appointment.start_at).where(
            Appointment.status == "booked",
            Appointment.superseded_at.is_(None),
            Appointment.service_request_id.is_not(None),
            Appointment.calendar_id.in_(calendar_ids),
            Appointment.start_at >= business_datetime(start),
            Appointment.start_at < business_datetime(end + timedelta(days=1)),
        )
    )
    for calendar_id, start_at in rows.tuples():
        counts[(calendar_id, to_business_date(start_at))] += 1

    tours = (
        await session.scalars(
            select(Tour).where(
                Tour.tour_date >= start, Tour.tour_date <= end, Tour.status != TourStatus.CANCELLED
            )
        )
    ).all()
    by_cell = {(t.calendar_id, t.tour_date): t for t in tours}
    result_rows = [
        OverviewRow(
            calendar=calendar_out(calendar),
            cells=[
                OverviewCell(
                    date=day,
                    appointment_count=counts.get((calendar.id, day), 0),
                    planned_count=len(by_cell[(calendar.id, day)].stops)
                    if (calendar.id, day) in by_cell
                    else 0,
                    tour=OverviewTour(
                        id=tour.id, status=tour.status, technician=user_ref(tour.technician)
                    )
                    if (tour := by_cell.get((calendar.id, day))) is not None
                    else None,
                )
                for day in days
            ],
        )
        for calendar in calendars
    ]
    technicians = (
        await session.scalars(
            select(User)
            .where(User.role == Role.TECHNICIAN, User.is_active.is_(True))
            .order_by(User.last_name, User.first_name)
        )
    ).all()
    load: dict[tuple[int, date], tuple[int, int]] = defaultdict(lambda: (0, 0))
    for tour in tours:
        if tour.technician_id is None:
            continue
        count, stops = load[(tour.technician_id, tour.tour_date)]
        load[(tour.technician_id, tour.tour_date)] = (count + 1, stops + len(tour.stops))
    tech_rows = []
    for tech in technicians:
        ref = user_ref(tech)
        assert ref is not None
        tech_rows.append(
            TechnicianLoad(
                technician=ref,
                days=[
                    TechnicianDay(
                        date=day,
                        tour_count=load[(tech.id, day)][0],
                        stop_count=load[(tech.id, day)][1],
                    )
                    for day in days
                ],
            )
        )
    return TourPlannerOverviewOut(days=days, rows=result_rows, technicians=tech_rows)


@router.get(
    "/my/tours",
    response_model=ListResponse[TourDetailOut],
    summary="My tours on a day",
    responses=_READ,
)
async def my_tours(
    session: SessionDep,
    user: Annotated[User, Depends(require("tours.execute_own"))],
    day: Annotated[date | None, Query(alias="date")] = None,
) -> ListResponse[TourDetailOut]:
    tours = await tours_on(session, day or business_today(), technician_id=user.id)
    return ListResponse[TourDetailOut](
        data=[await build_tour_detail(session, t, user) for t in tours]
    )


@router.get(
    "/service-requests/{sr_id}/tour-stops",
    response_model=ListResponse[TourStopRef],
    summary="Tour stops of a service request",
    responses=_READ,
)
async def sr_tour_stops(
    sr_id: int,
    session: SessionDep,
    _user: Annotated[User, Depends(require("tours.read"))],
) -> ListResponse[TourStopRef]:
    if (
        await session.scalar(
            select(func.count(ServiceRequest.id)).where(ServiceRequest.id == sr_id)
        )
        == 0
    ):
        raise NotFoundError(f"Service request {sr_id} not found.")
    stops = (
        await session.scalars(
            select(TourStop)
            .join(Tour, Tour.id == TourStop.tour_id)
            .where(TourStop.service_request_id == sr_id)
            .order_by(Tour.tour_date.desc(), TourStop.id.desc())
        )
    ).all()
    return ListResponse[TourStopRef](
        data=[
            TourStopRef(
                tour_id=s.tour_id,
                stop_id=s.id,
                tour_date=s.tour.tour_date,
                technician_name=s.tour.technician.display_name if s.tour.technician else None,
                status=s.tour.status,
                outcome=s.outcome,
            )
            for s in stops
        ]
    )
