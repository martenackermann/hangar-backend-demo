"""Tour planning and tour state (SPEC §4.7, §6.12; SPEC_RESOLUTIONS #8, #32).

Services mutate and never commit; routers call ``commit()``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import can
from app.core.enums import (
    WITH_APPT,
    Readiness,
    Role,
    TourStatus,
    TourStopOutcome,
)
from app.core.errors import (
    ApiError,
    ConflictError,
    GuardFailed,
    NotFoundError,
    TransitionNotAllowed,
    ValidationProblem,
)
from app.core.loading import ensure_loaded
from app.core.models import Region, ServiceRequest, User
from app.core.notifications import notify
from app.core.read_models import build_sr_list_items, region_ref, user_ref
from app.core.schemas import DeliveryAddress, EntityRef
from app.core.timeutil import business_today, now_utc, to_business_date

from .common import (
    GUARD_NO_PENDING_STOPS,
    GUARD_TOUR_DATE_TODAY,
    NOT_OWN_TOUR,
    can_execute_tour,
    day_bounds,
    is_new_slot,
    item_label,
    readiness_of,
)
from .models import BookingCalendar, Tour, TourStop
from .schemas import (
    CalendarWithRegion,
    StopContact,
    StopItem,
    StopServiceRequest,
    TourCandidate,
    TourCandidatesOut,
    TourDetailOut,
    TourStopOut,
    TourSummaryOut,
)

TS = TourStatus
OUT = TourStopOutcome
EDITABLE = frozenset({TS.DRAFT, TS.PLANNED})
STOPS_EDITABLE = frozenset({TS.DRAFT, TS.PLANNED, TS.IN_PROGRESS})


# --- read models ------------------------------------------------------------------------------


def calendar_out(calendar: BookingCalendar) -> CalendarWithRegion:
    return CalendarWithRegion(
        id=calendar.id, name=calendar.name, region=region_ref(calendar.region)
    )


def build_stop(stop: TourStop) -> TourStopOut:
    sr = stop.service_request
    contact = sr.contact
    return TourStopOut(
        id=stop.id,
        position=stop.position,
        outcome=stop.outcome,
        outcome_at=stop.outcome_at,
        readiness=readiness_of(sr.status),
        service_request=StopServiceRequest(
            id=sr.id,
            number=sr.number,
            status=sr.status,
            delivery_type=sr.effective_delivery_type,
            is_campus=sr.is_campus,
            is_vip=sr.is_vip,
            contact=StopContact(
                id=contact.id,
                user_id=contact.user_id,
                display_name=contact.display_name,
                email=contact.email,
                phone=contact.phone,
                mobile=contact.mobile,
            ),
            onsite_contact_phone=sr.onsite_contact_phone,
            delivery_address=DeliveryAddress(
                street=sr.delivery_street,
                zip=sr.delivery_zip,
                city=sr.delivery_city,
                country=sr.delivery_country,
                room=sr.delivery_room,
                building=sr.delivery_building,
            ),
            appointment_start_at=sr.appointment_start_at,
            appointment_end_at=sr.appointment_end_at,
            new_items=[
                StopItem(slot=i.slot, label=item_label(i), verified=i.verified_at is not None)
                for i in sr.items
                if is_new_slot(i.slot)
            ],
            old_items=[
                StopItem(slot=i.slot, label=item_label(i), verified=i.verified_at is not None)
                for i in sr.items
                if not is_new_slot(i.slot)
            ],
        ),
    )


def available_tour_actions(tour: Tour, user: User | None, today: date | None = None) -> list[str]:
    """Tour actions the user can run now (API.md §7.2 ``availableActions``)."""
    if user is None:
        return []
    today = today or business_today()
    result: list[str] = []
    pending = any(s.outcome == OUT.PENDING for s in tour.stops)
    if (
        tour.status == TS.PLANNED
        and tour.tour_date == today
        and tour.technician_id is not None
        and can_execute_tour(user, tour)
    ):
        result.append("start")
    if tour.status == TS.IN_PROGRESS and not pending and can_execute_tour(user, tour):
        result.append("finish")
    if can(user, "tours.plan"):
        if tour.status in EDITABLE:
            result.append("cancel")
            result.append("assign_technician")
        if tour.status == TS.PLANNED:
            result.append("unassign_technician")
    return result


def _summary_fields(tour: Tour, user: User | None) -> dict[str, object]:
    stops = list(tour.stops)
    counts = Counter(str(s.outcome) for s in stops)
    readiness = dict.fromkeys(Readiness, 0)
    for stop in stops:
        if stop.outcome == OUT.PENDING:
            readiness[readiness_of(stop.service_request.status)] += 1
    starts = [
        s.service_request.appointment_start_at
        for s in stops
        if s.service_request.appointment_start_at is not None
    ]
    return {
        "id": tour.id,
        "calendar": calendar_out(tour.calendar),
        "tour_date": tour.tour_date,
        "technician": user_ref(tour.technician),
        "status": tour.status,
        "stop_count": len(stops),
        "pending_count": counts.get(OUT.PENDING, 0),
        "completed_count": counts.get(OUT.COMPLETED, 0),
        "rescheduled_count": counts.get(OUT.RESCHEDULED, 0),
        "cancelled_count": counts.get(OUT.CANCELLED, 0),
        "first_appointment_at": min(starts) if starts else None,
        "last_appointment_at": max(starts) if starts else None,
        "readiness": readiness,
        "available_actions": available_tour_actions(tour, user),
    }


def build_tour_summary(tour: Tour, user: User | None) -> TourSummaryOut:
    return TourSummaryOut(**_summary_fields(tour, user))  # type: ignore[arg-type]


async def tour_warnings(session: AsyncSession, tour: Tour) -> list[str]:
    """Non-blocking warnings (API.md §7.2)."""
    warnings: list[str] = []
    if tour.technician_id is not None and tour.status != TS.CANCELLED:
        other = await session.scalar(
            select(func.count(Tour.id)).where(
                Tour.technician_id == tour.technician_id,
                Tour.tour_date == tour.tour_date,
                Tour.status != TS.CANCELLED,
                Tour.id != tour.id,
            )
        )
        if other:
            warnings.append("technician_has_other_tour")
    pending = [s for s in tour.stops if s.outcome == OUT.PENDING]
    if any(readiness_of(s.service_request.status) != Readiness.READY for s in pending):
        warnings.append("stops_not_ready")
    for stop in pending:
        start = stop.service_request.appointment_start_at
        calendar_id = stop.service_request.appointment_calendar_id
        if (
            start is None
            or to_business_date(start) != tour.tour_date
            or calendar_id != tour.calendar_id
        ):
            warnings.append("appointment_outside_tour_date")
            break
    return warnings


async def build_tour_detail(
    session: AsyncSession, tour: Tour, user: User | None, warnings: list[str] | None = None
) -> TourDetailOut:
    await ensure_loaded(session, tour, "stops", "calendar", "technician")
    return TourDetailOut(
        **_summary_fields(tour, user),  # type: ignore[arg-type]
        notes=tour.notes,
        started_at=tour.started_at,
        finished_at=tour.finished_at,
        stops=[build_stop(s) for s in tour.stops],
        warnings=warnings if warnings is not None else await tour_warnings(session, tour),
        version=tour.version,
    )


# --- loading ----------------------------------------------------------------------------------


async def load_tour(session: AsyncSession, tour_id: int, *, for_update: bool = False) -> Tour:
    stmt = select(Tour).where(Tour.id == tour_id).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update(of=Tour)
    tour = (await session.scalars(stmt)).first()
    if tour is None:
        raise NotFoundError(f"Tour {tour_id} not found.")
    await ensure_loaded(session, tour, "stops", "calendar", "technician")
    return tour


async def load_calendar(
    session: AsyncSession, calendar_id: int, field: str = "calendarId"
) -> BookingCalendar:
    calendar = await session.get(BookingCalendar, calendar_id)
    if calendar is None:
        raise ValidationProblem({field: ["Unknown booking calendar."]})
    return calendar


async def load_technician(session: AsyncSession, technician_id: int) -> User:
    technician = await session.get(User, technician_id)
    if technician is None or not technician.is_active or technician.role != Role.TECHNICIAN:
        raise ValidationProblem(
            {"technicianId": ["Choose an active user with the technician role."]}
        )
    return technician


def check_tour_version(tour: Tour, version: int | None) -> None:
    from app.core.transitions import check_version

    check_version(tour, version)


# --- candidates -------------------------------------------------------------------------------


async def candidate_srs(
    session: AsyncSession, calendar_id: int, day: date, tour: Tour | None
) -> list[tuple[ServiceRequest, int | None]]:
    """SRs with their current appointment in ``calendar`` on ``day`` (status ``WITH_APPT``),
    not pending in another tour; plus SRs pending in ``tour`` itself (SPEC §6.12)."""
    start, end = day_bounds(day)
    rows = (
        await session.scalars(
            select(ServiceRequest)
            .where(
                ServiceRequest.appointment_calendar_id == calendar_id,
                ServiceRequest.appointment_start_at >= start,
                ServiceRequest.appointment_start_at < end,
                ServiceRequest.status.in_(WITH_APPT),
            )
            .order_by(ServiceRequest.appointment_start_at, ServiceRequest.id)
        )
    ).all()
    pending = dict(
        (
            await session.execute(
                select(TourStop.service_request_id, TourStop.tour_id).where(
                    TourStop.outcome == OUT.PENDING
                )
            )
        )
        .tuples()
        .all()
    )
    tour_id = tour.id if tour is not None else None
    result: list[tuple[ServiceRequest, int | None]] = []
    seen: set[int] = set()
    for sr in rows:
        in_tour = pending.get(sr.id)
        if in_tour is not None and in_tour != tour_id:
            continue
        result.append((sr, in_tour))
        seen.add(sr.id)
    if tour is not None:
        for stop in tour.stops:
            if stop.outcome == OUT.PENDING and stop.service_request_id not in seen:
                result.append((stop.service_request, tour.id))
    return result


async def active_tour(session: AsyncSession, calendar_id: int, day: date) -> Tour | None:
    tour = (
        await session.scalars(
            select(Tour).where(
                Tour.calendar_id == calendar_id,
                Tour.tour_date == day,
                Tour.status != TS.CANCELLED,
            )
        )
    ).first()
    if tour is not None:
        await ensure_loaded(session, tour, "stops", "calendar", "technician")
    return tour


async def build_candidates(
    session: AsyncSession, calendar: BookingCalendar, day: date, user: User
) -> TourCandidatesOut:
    tour = await active_tour(session, calendar.id, day)
    pairs = await candidate_srs(session, calendar.id, day, tour)
    items = await build_sr_list_items(session, [sr for sr, _ in pairs], user)
    return TourCandidatesOut(
        calendar=calendar_out(calendar),
        date=day,
        tour=build_tour_summary(tour, user) if tour is not None else None,
        candidates=[
            TourCandidate(service_request=item, readiness=readiness_of(sr.status), in_tour_id=tid)
            for item, (sr, tid) in zip(items, pairs, strict=True)
        ],
    )


async def _resolve_stop_srs(
    session: AsyncSession, tour: Tour, calendar_id: int, day: date, sr_ids: Sequence[int]
) -> list[ServiceRequest]:
    if len(set(sr_ids)) != len(sr_ids):
        raise ValidationProblem({"serviceRequestIds": ["Each request may appear only once."]})
    allowed = {sr.id: sr for sr, _ in await candidate_srs(session, calendar_id, day, tour)}
    missing = [i for i in sr_ids if i not in allowed]
    if missing:
        numbers = (
            await session.scalars(
                select(ServiceRequest.number).where(ServiceRequest.id.in_(missing))
            )
        ).all()
        label = ", ".join(numbers) or ", ".join(str(i) for i in missing)
        raise ConflictError(
            f"Not a candidate for this route and day: {label}.",
            serviceRequestIds=missing,
        )
    return [allowed[i] for i in sr_ids]


# --- mutations --------------------------------------------------------------------------------


async def notify_assigned(session: AsyncSession, tour: Tour, actor: User | None) -> None:
    """``tour.assigned`` to the (new) technician (SPEC §10)."""
    await ensure_loaded(session, tour, "stops", "calendar", "technician")
    technician = tour.technician
    if technician is None or not technician.email:
        return
    stops = [s for s in tour.stops if s.outcome == OUT.PENDING]
    await notify(
        session,
        "tour.assigned",
        to=[technician.email],
        context={
            "technician_first_name": technician.first_name,
            "calendar_name": tour.calendar.name,
            "tour_date": tour.tour_date.isoformat(),
            "stop_count": len(stops),
            "stops": [
                {
                    "time": s.service_request.appointment_start_at.isoformat()
                    if s.service_request.appointment_start_at
                    else None,
                    "sr_number": s.service_request.number,
                    "town": s.service_request.delivery_city or "",
                }
                for s in stops
            ],
            "my_tour_path": f"/field/my-tour?date={tour.tour_date.isoformat()}",
        },
        entity=EntityRef(type="tour", id=tour.id, label=f"{tour.calendar.name} {tour.tour_date}"),
        actor=actor,
    )


async def create_tour(
    session: AsyncSession,
    *,
    calendar_id: int,
    tour_date: date,
    technician_id: int | None,
    service_request_ids: Sequence[int],
    notes: str | None,
    actor: User,
) -> Tour:
    calendar = await load_calendar(session, calendar_id)
    existing = await active_tour(session, calendar.id, tour_date)
    if existing is not None:
        raise ApiError(
            f"A tour for {calendar.name} on {tour_date.isoformat()} already exists (tour {existing.id}).",
            status=409,
            code="tour_exists",
            title="Tour exists",
            tourId=existing.id,
        )
    technician = await load_technician(session, technician_id) if technician_id else None
    tour = Tour(
        calendar=calendar,
        calendar_id=calendar.id,
        tour_date=tour_date,
        technician=technician,
        technician_id=technician.id if technician else None,
        status=TS.PLANNED if technician else TS.DRAFT,
        notes=notes,
        created_by=actor.id,
        stops=[],
    )
    session.add(tour)
    srs = await _resolve_stop_srs(session, tour, calendar.id, tour_date, service_request_ids)
    for position, sr in enumerate(srs, start=1):
        tour.stops.append(TourStop(service_request=sr, service_request_id=sr.id, position=position))
    await session.flush()
    if technician is not None:
        await notify_assigned(session, tour, actor)
    return tour


async def change_technician(
    session: AsyncSession, tour: Tour, technician_id: int | None, actor: User
) -> None:
    """``assign_technician`` / ``unassign_technician`` (SPEC §4.7)."""
    if technician_id == tour.technician_id:
        return
    if technician_id is None:
        if tour.status != TS.PLANNED:
            raise TransitionNotAllowed(
                f"Action 'unassign_technician' is not allowed while the tour is '{tour.status}'.",
                action="unassign_technician",
                current_status=tour.status,
                allowed_actions=available_tour_actions(tour, actor),
            )
        tour.technician = None
        tour.technician_id = None
        tour.status = TS.DRAFT
        return
    if tour.status not in EDITABLE:
        raise TransitionNotAllowed(
            f"Action 'assign_technician' is not allowed while the tour is '{tour.status}'.",
            action="assign_technician",
            current_status=tour.status,
            allowed_actions=available_tour_actions(tour, actor),
        )
    technician = await load_technician(session, technician_id)
    tour.technician = technician
    tour.technician_id = technician.id
    tour.status = TS.PLANNED
    await session.flush()
    await notify_assigned(session, tour, actor)


def _renumber(tour: Tour, pending_order: Sequence[TourStop] | None = None) -> None:
    done = [s for s in tour.stops if s.outcome != OUT.PENDING]
    pending = (
        list(pending_order)
        if pending_order is not None
        else [s for s in tour.stops if s.outcome == OUT.PENDING]
    )
    done.sort(key=lambda s: s.position)
    for position, stop in enumerate([*done, *pending], start=1):
        stop.position = position


async def set_stops(
    session: AsyncSession, tour: Tour, service_request_ids: Sequence[int], actor: User
) -> None:
    """``PUT /tours/{id}/stops``: complete ordered list of **pending** stops."""
    if tour.status not in STOPS_EDITABLE:
        raise ConflictError(f"Stops of a {tour.status} tour cannot be changed.")
    srs = await _resolve_stop_srs(
        session, tour, tour.calendar_id, tour.tour_date, service_request_ids
    )
    current = {s.service_request_id: s for s in tour.stops if s.outcome == OUT.PENDING}
    order: list[TourStop] = []
    for sr in srs:
        stop = current.pop(sr.id, None)
        if stop is None:
            stop = TourStop(service_request=sr, service_request_id=sr.id, position=0)
            tour.stops.append(stop)
        order.append(stop)
    for stop in current.values():
        tour.stops.remove(stop)
    _renumber(tour, order)
    await session.flush()


async def start_tour(session: AsyncSession, tour: Tour, actor: User) -> None:
    if tour.status != TS.PLANNED:
        raise TransitionNotAllowed(
            f"Action 'start' is not allowed while the tour is '{tour.status}'.",
            action="start",
            current_status=tour.status,
            allowed_actions=available_tour_actions(tour, actor),
        )
    if tour.tour_date != business_today():
        raise GuardFailed(GUARD_TOUR_DATE_TODAY, "A tour can only be started on its tour date.")
    tour.status = TS.IN_PROGRESS
    tour.started_at = now_utc()
    await session.flush()


async def ensure_running(session: AsyncSession, tour: Tour, actor: User) -> None:
    """SPEC_RESOLUTIONS #32: the first stop action on a planned tour auto-starts it."""
    if tour.status == TS.PLANNED:
        await start_tour(session, tour, actor)


def maybe_complete(tour: Tour) -> None:
    """Auto-complete when no pending stop remains (SPEC §4.7, SPEC_RESOLUTIONS #32)."""
    if tour.status not in STOPS_EDITABLE:
        return
    stops = list(tour.stops)
    if not stops or any(s.outcome == OUT.PENDING for s in stops):
        return
    tour.status = TS.COMPLETED
    tour.finished_at = now_utc()


async def set_outcome(session: AsyncSession, stop: TourStop, outcome: TourStopOutcome) -> None:
    stop.outcome = outcome
    stop.outcome_at = now_utc()
    tour = stop.tour
    await ensure_loaded(session, tour, "stops")
    maybe_complete(tour)
    await session.flush()


async def remove_stop(session: AsyncSession, stop: TourStop) -> None:
    tour = stop.tour
    await ensure_loaded(session, tour, "stops")
    tour.stops.remove(stop)
    _renumber(tour)
    await session.flush()


async def release_pending_stop(session: AsyncSession, sr_id: int, outcome: TourStopOutcome) -> None:
    """SPEC_RESOLUTIONS #8: in an ``in_progress`` tour the stop keeps an outcome; otherwise
    the pending stop is deleted (the SR becomes unplanned)."""
    stop = (
        await session.scalars(
            select(TourStop).where(
                TourStop.service_request_id == sr_id, TourStop.outcome == OUT.PENDING
            )
        )
    ).first()
    if stop is None:
        return
    if stop.tour.status == TS.IN_PROGRESS:
        await set_outcome(session, stop, outcome)
    else:
        await remove_stop(session, stop)


async def drop_moved_stop(session: AsyncSession, sr: ServiceRequest) -> None:
    """Sync moved the appointment: a pending stop on another date/calendar is deleted unless
    its tour is already on the road (SPEC §6.12)."""
    stop = (
        await session.scalars(
            select(TourStop).where(
                TourStop.service_request_id == sr.id, TourStop.outcome == OUT.PENDING
            )
        )
    ).first()
    if stop is None or stop.tour.status == TS.IN_PROGRESS:
        return
    start = sr.appointment_start_at
    if (
        start is None
        or to_business_date(start) != stop.tour.tour_date
        or sr.appointment_calendar_id != stop.tour.calendar_id
    ):
        await remove_stop(session, stop)


async def finish_tour(session: AsyncSession, tour: Tour, actor: User) -> None:
    if tour.status != TS.IN_PROGRESS:
        raise TransitionNotAllowed(
            f"Action 'finish' is not allowed while the tour is '{tour.status}'.",
            action="finish",
            current_status=tour.status,
            allowed_actions=available_tour_actions(tour, actor),
        )
    if any(s.outcome == OUT.PENDING for s in tour.stops):
        raise GuardFailed(GUARD_NO_PENDING_STOPS, "Every stop needs an outcome first.")
    tour.status = TS.COMPLETED
    tour.finished_at = now_utc()
    await session.flush()


async def cancel_tour(session: AsyncSession, tour: Tour, actor: User) -> None:
    if tour.status not in EDITABLE:
        raise TransitionNotAllowed(
            f"Action 'cancel' is not allowed while the tour is '{tour.status}'.",
            action="cancel",
            current_status=tour.status,
            allowed_actions=available_tour_actions(tour, actor),
        )
    for stop in [s for s in tour.stops if s.outcome == OUT.PENDING]:
        tour.stops.remove(stop)
    _renumber(tour)
    tour.status = TS.CANCELLED
    await session.flush()


def ensure_tour_executor(tour: Tour, user: User) -> None:
    from app.core.errors import ForbiddenError

    if not can_execute_tour(user, tour):
        raise ForbiddenError("This tour belongs to another technician.", code=NOT_OWN_TOUR)


async def run_tour_action(session: AsyncSession, tour: Tour, action: str, actor: User) -> None:
    from app.core.errors import ForbiddenError

    if action in ("start", "finish"):
        if not (can(actor, "tours.execute_own") or can(actor, "tours.execute_any")):
            raise ForbiddenError("You do not have permission to perform this action.")
        ensure_tour_executor(tour, actor)
        if action == "start":
            await start_tour(session, tour, actor)
        else:
            await finish_tour(session, tour, actor)
    elif action == "cancel":
        if not can(actor, "tours.plan"):
            raise ForbiddenError("You do not have permission to perform this action.")
        await cancel_tour(session, tour, actor)
    else:
        raise NotFoundError(f"Unknown action '{action}'.", code="unknown_action")


# --- my tours / overview helpers --------------------------------------------------------------


async def tours_on(
    session: AsyncSession, day: date, *, technician_id: int | None = None
) -> list[Tour]:
    stmt = select(Tour).where(Tour.tour_date == day, Tour.status != TS.CANCELLED)
    if technician_id is not None:
        stmt = stmt.where(Tour.technician_id == technician_id)
    tours = (await session.scalars(stmt.order_by(Tour.calendar_id, Tour.id))).all()
    return list(tours)


def working_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


async def calendars_for(
    session: AsyncSession, region_ids: Sequence[int] | None
) -> list[BookingCalendar]:
    stmt = select(BookingCalendar).outerjoin(Region, Region.id == BookingCalendar.region_id)
    if region_ids:
        stmt = stmt.where(BookingCalendar.region_id.in_(region_ids))
    stmt = stmt.order_by(BookingCalendar.name, BookingCalendar.id)
    return list((await session.scalars(stmt)).all())
