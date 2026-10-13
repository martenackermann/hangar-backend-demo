"""H4 seeder (order 40) — SPEC §11.6, SPEC_RESOLUTIONS #6, #41–#47.

Reads the core story (never invents SR statuses): calendars 1–9, one booked current
appointment per SR with ``appointment_*`` set, the reschedule history of the
``reschedule_needed`` SRs, 5 unmatched appointments next week, sandbox rows mirroring all
of them, the sync state, tours from Monday of D0's week (or wd(−1)) to Friday of the next
week, and protocol PDFs for every completed SR.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import BOOKING_CALENDARS, BOOKING_SERVICE_ID
from app.core.enums import (
    WITH_APPT,
    AppointmentStatus,
    ServiceRequestStatus,
    TourStatus,
    TourStopOutcome,
)
from app.core.gateways.fakes.booking import FakeBookingProvider, appointment_external_id
from app.core.gateways.protocols import ProviderAppointment
from app.core.models import Region, ServiceRequest, StatusTransition
from app.core.seed.framework import SeedContext
from app.core.seed.story import TECHNICIAN_BY_REGION
from app.core.seed.vocabulary import FIRST_NAMES, LAST_NAMES
from app.core.timeutil import to_business_date, week_monday

from .models import Appointment, BookingCalendar, BookingSyncState, Tour, TourStop
from .protocol import generate_protocol

log = structlog.get_logger(__name__)

SR = ServiceRequestStatus
OUT = TourStopOutcome
DANA = "dana.dispatcher@example.com"
UNMATCHED_REFERENCES = ("SR001230", None, "SR00I2047", "SR 0012188", "Hardware swap")


async def seed_field(session: AsyncSession, ctx: SeedContext) -> None:
    if await session.scalar(select(func.count()).select_from(BookingCalendar)):
        return
    synced = ctx.now - timedelta(minutes=40)
    calendars = await _calendars(session, synced)
    core: dict[str, Any] = ctx.data.get("core", {})
    rows = await _appointments(session, ctx, calendars, core, synced)
    max_seq = await FakeBookingProvider().mirror(
        session, [_to_provider(row, calendars[row.calendar_id]) for row in rows]
    )
    session.add(
        BookingSyncState(
            id=1,
            sync_token=str(max_seq),
            last_full_sync_at=synced - timedelta(days=1),
            last_incremental_sync_at=synced,
            last_result={
                "calendars": len(calendars),
                "created": 4,
                "updated": 1,
                "cancelled": 1,
                "matched": 4,
                "unmatched": 0,
                "transitions": 3,
            },
        )
    )
    await session.flush()
    await _tours(session, ctx, calendars, core)
    await _protocols(session)
    log.info("h4_seeded", appointments=len(rows))


# --- calendars --------------------------------------------------------------------------------


async def _calendars(session: AsyncSession, synced: datetime) -> dict[int, BookingCalendar]:
    regions = {r.code: r for r in (await session.scalars(select(Region))).all()}
    calendars: dict[int, BookingCalendar] = {}
    for definition in BOOKING_CALENDARS:
        region = regions.get(definition.region_code)
        calendar = BookingCalendar(
            id=definition.id,
            external_id=definition.external_id,
            name=definition.name,
            region_id=region.id if region else None,
            service_ids=[BOOKING_SERVICE_ID],
            calendar_type="route",
            is_enabled=True,
            synced_at=synced,
        )
        session.add(calendar)
        calendars[definition.id] = calendar
    await session.flush()
    await session.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('booking_calendars', 'id'), "
            "(SELECT max(id) FROM booking_calendars))"
        )
    )
    return calendars


# --- appointments -----------------------------------------------------------------------------


async def _events(session: AsyncSession, actions: tuple[str, ...]) -> dict[int, StatusTransition]:
    """Latest transition per SR among ``actions``."""
    records = (
        await session.scalars(
            select(StatusTransition)
            .where(
                StatusTransition.entity_type == "service_request",
                StatusTransition.action.in_(actions),
            )
            .order_by(StatusTransition.occurred_at)
        )
    ).all()
    return {r.entity_id: r for r in records}


async def _appointments(
    session: AsyncSession,
    ctx: SeedContext,
    calendars: dict[int, BookingCalendar],
    core: dict[str, Any],
    synced: datetime,
) -> list[Appointment]:
    booked = await _events(session, ("appointment_synced", "book_appointment"))
    rows: list[Appointment] = []
    srs = (
        await session.scalars(
            select(ServiceRequest)
            .where(ServiceRequest.appointment_start_at.is_not(None))
            .order_by(ServiceRequest.id)
        )
    ).all()
    for sr in srs:
        start = sr.appointment_start_at
        calendar_id = sr.appointment_calendar_id
        if start is None or calendar_id not in calendars:
            continue
        event = booked.get(sr.id)
        booked_at = event.occurred_at if event else min(start, ctx.now) - timedelta(days=7)
        rows.append(
            Appointment(
                external_id=appointment_external_id(sr.number, start),
                calendar_id=calendar_id,
                calendar=calendars[calendar_id],
                service_request_id=sr.id,
                reference=sr.number,
                status=AppointmentStatus.BOOKED,
                start_at=start,
                end_at=sr.appointment_end_at or start + timedelta(minutes=60),
                title=f"Hardware rollout visit {sr.number}",
                contact_name=sr.contact.display_name,
                contact_email=sr.contact.email,
                street=sr.delivery_street,
                town=sr.delivery_city,
                booked_at=booked_at,
                provider_updated_at=booked_at,
                source="manual"
                if event is not None and event.action == "book_appointment"
                else "sync",
                last_synced_at=synced,
                is_current=True,
            )
        )

    on_site = await _events(session, ("reschedule_on_site",))
    for raw_id, history in sorted(core.get("reschedule_history", {}).items()):
        sr = await session.get(ServiceRequest, int(raw_id))
        calendar = calendars.get(int(history["calendar"]))
        if sr is None or calendar is None:
            continue
        start = datetime.fromisoformat(history["start"])
        booked_at = min(start, ctx.now) - timedelta(days=8)
        cancelled = history.get("kind") != "on_site"
        changed_at = (
            datetime.fromisoformat(history["cancelled_at"])
            if cancelled and history.get("cancelled_at")
            else (on_site[sr.id].occurred_at if sr.id in on_site else start + timedelta(minutes=20))
        )
        rows.append(
            Appointment(
                external_id=appointment_external_id(sr.number, start),
                calendar_id=calendar.id,
                calendar=calendar,
                service_request_id=sr.id,
                reference=sr.number,
                status=AppointmentStatus.CANCELLED if cancelled else AppointmentStatus.BOOKED,
                start_at=start,
                end_at=start + timedelta(minutes=60),
                title=f"Hardware rollout visit {sr.number}",
                contact_name=sr.contact.display_name,
                contact_email=sr.contact.email,
                street=sr.delivery_street,
                town=sr.delivery_city,
                notes="Cancelled by the user in the booking portal" if cancelled else None,
                booked_at=booked_at,
                provider_updated_at=changed_at,
                source="sync",
                last_synced_at=synced,
                is_current=False,
                superseded_at=None if cancelled else changed_at,
            )
        )

    rng = ctx.rng
    # fixed calendar ids (regions sorted by name): eastgate, hillford, westbay, lakeside, riverton
    unmatched_calendars = [1, 3, 9, 4, 7]
    for n, reference in enumerate(UNMATCHED_REFERENCES):
        day = ctx.wd(5 + n)
        calendar = calendars[unmatched_calendars[n % len(unmatched_calendars)]]
        start = ctx.at(day, 8 + rng.randint(0, 7), 30)
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        rows.append(
            Appointment(
                external_id=appointment_external_id(f"unmatched-{n}-{reference}", start),
                calendar_id=calendar.id,
                calendar=calendar,
                service_request_id=None,
                reference=reference,
                status=AppointmentStatus.BOOKED,
                start_at=start,
                end_at=start + timedelta(minutes=60),
                title=f"Hardware rollout visit {reference}"
                if reference
                else "Hardware rollout visit",
                contact_name=name,
                contact_email=f"{name.lower().replace(' ', '.')}@example.com",
                street=None,
                town=calendar.name.replace(" route", ""),
                notes="Reference typed by the user" if reference else "No order number given",
                booked_at=ctx.now - timedelta(days=rng.randint(1, 4), hours=rng.randint(0, 8)),
                provider_updated_at=None,
                source="sync",
                last_synced_at=synced,
                is_current=False,
            )
        )
    session.add_all(rows)
    await session.flush()
    return rows


def _to_provider(row: Appointment, calendar: BookingCalendar) -> ProviderAppointment:
    stamp = row.booked_at or row.start_at
    return ProviderAppointment(
        external_id=row.external_id,
        calendar_external_id=calendar.external_id,
        start_at=row.start_at,
        end_at=row.end_at,
        status="cancelled" if row.status == AppointmentStatus.CANCELLED else "booked",
        reference=row.reference,
        title=row.title,
        contact_name=row.contact_name,
        contact_email=row.contact_email,
        street=row.street,
        town=row.town,
        notes=row.notes,
        created_at=stamp,
        updated_at=row.provider_updated_at or stamp,
    )


# --- tours ------------------------------------------------------------------------------------


def _days(start: date, end: date) -> list[date]:
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


async def _tours(
    session: AsyncSession,
    ctx: SeedContext,
    calendars: dict[int, BookingCalendar],
    core: dict[str, Any],
) -> None:
    from app.core.models import User

    users = {u.email: u for u in (await session.scalars(select(User))).all()}
    dana = users[DANA]
    region_code = {d.id: d.region_code for d in BOOKING_CALENDARS}
    d0 = ctx.d0
    monday = week_monday(d0)
    first = min(monday, ctx.wd(-1))
    last = monday + timedelta(days=11)
    next_week = monday + timedelta(days=7)
    theo_today = set(core.get("theo_today_sr_ids", []))
    rescheduled_on_site = await _events(session, ("reschedule_on_site",))

    # (calendar_id, day) -> [(sr, outcome, outcome_at, start)]
    cells: dict[tuple[int, date], list[tuple[ServiceRequest, OUT, datetime | None, datetime]]] = {}
    srs = (
        await session.scalars(
            select(ServiceRequest)
            .where(ServiceRequest.appointment_start_at.is_not(None))
            .order_by(ServiceRequest.appointment_start_at, ServiceRequest.id)
        )
    ).all()
    for sr in srs:
        start = sr.appointment_start_at
        assert start is not None
        day = to_business_date(start)
        calendar_id = sr.appointment_calendar_id
        if calendar_id is None or not first <= day <= last:
            continue
        if day < d0:
            if sr.status == SR.COMPLETED:
                entry = (sr, OUT.COMPLETED, sr.completed_at, start)
            elif sr.status == SR.CANCELLED:
                entry = (sr, OUT.CANCELLED, sr.cancelled_at, start)
            else:
                continue  # overdue SRs stay out of tours (SPEC_RESOLUTIONS #46)
        else:
            if sr.status not in WITH_APPT:
                continue
            if day == d0 and region_code.get(calendar_id) == "riverton" and sr.id not in theo_today:
                continue  # Theo's tour today has exactly his 5 stops
            entry = (sr, OUT.PENDING, None, start)
        cells.setdefault((calendar_id, day), []).append(entry)
    for raw_id, history in core.get("reschedule_history", {}).items():
        if history.get("kind") != "on_site":
            continue
        sr = await session.get(ServiceRequest, int(raw_id))
        start = datetime.fromisoformat(history["start"])
        day = to_business_date(start)
        if sr is None or not first <= day < d0:
            continue
        event = rescheduled_on_site.get(sr.id)
        cells.setdefault((int(history["calendar"]), day), []).append(
            (sr, OUT.RESCHEDULED, event.occurred_at if event else start, start)
        )

    rng = ctx.rng
    for (calendar_id, day), entries in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        entries.sort(key=lambda e: (e[3], e[0].id))
        technician = users[TECHNICIAN_BY_REGION[region_code[calendar_id]]]
        if day < d0:
            status = TourStatus.COMPLETED
        elif day >= next_week and rng.random() < 0.3:
            status = TourStatus.DRAFT
        else:
            status = TourStatus.PLANNED
        assigned = None if status == TourStatus.DRAFT else technician
        tour = Tour(
            calendar_id=calendar_id,
            calendar=calendars[calendar_id],
            tour_date=day,
            technician_id=assigned.id if assigned else None,
            technician=assigned,
            status=status,
            created_by=dana.id,
            started_at=entries[0][3] - timedelta(minutes=30)
            if status == TourStatus.COMPLETED
            else None,
            finished_at=max((e[2] for e in entries if e[2] is not None), default=None)
            if status == TourStatus.COMPLETED
            else None,
            stops=[
                TourStop(
                    service_request_id=sr.id,
                    service_request=sr,
                    position=position,
                    outcome=outcome,
                    outcome_at=outcome_at,
                )
                for position, (sr, outcome, outcome_at, _start) in enumerate(entries, start=1)
            ],
        )
        session.add(tour)
    await session.flush()


# --- protocols (SPEC_RESOLUTIONS #6) ----------------------------------------------------------


async def _protocols(session: AsyncSession) -> None:
    srs = (
        await session.scalars(
            select(ServiceRequest)
            .where(ServiceRequest.status == SR.COMPLETED, ServiceRequest.protocol_file_id.is_(None))
            .order_by(ServiceRequest.id)
        )
    ).all()
    for sr in srs:
        await generate_protocol(session, sr)
    await session.flush()
