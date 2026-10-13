"""Booking calendars, appointments and the provider sync (SPEC §6.18, §8.1; SPEC_RESOLUTIONS #7).

``reconcile_sr`` is the single place that turns appointment rows into SR transitions:
``appointment_synced`` (PRE_APPT → booked), moved appointments (update ``appointment_*``,
drop a stale pending stop) and ``appointment_cancelled`` (→ ``reschedule_needed``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import BOOKING_SERVICE_ID
from app.core.enums import (
    PRE_APPT,
    SR_TERMINAL,
    WITH_APPT,
    AppointmentStatus,
    JobKind,
    JobStatus,
    ServiceRequestStatus,
    Trigger,
)
from app.core.gateways.protocols import BookingProvider, BookingProviderError, ProviderAppointment
from app.core.models import BackgroundJob, Region, ServiceRequest, StatusTransition, User
from app.core.services import set_appointment
from app.core.timeutil import business_today, now_utc
from app.core.transitions import transition_service

from .models import Appointment, BookingCalendar, BookingSyncState
from .reschedule import reschedule_effects
from .schemas import (
    AppointmentOut,
    AppointmentSrRef,
    BookingCalendarOut,
    CalendarRef,
    SyncCounts,
)
from .tours import drop_moved_stop

log = structlog.get_logger(__name__)

SR = ServiceRequestStatus
APPT = AppointmentStatus
PAGE = 200
FULL_SYNC_DAYS = 30
_CAL_RE = re.compile(r"^cal-([a-z0-9_-]+)$")


# --- read models ------------------------------------------------------------------------------


async def upcoming_counts(session: AsyncSession) -> dict[int, int]:
    rows = await session.execute(
        select(Appointment.calendar_id, func.count(Appointment.id))
        .where(
            Appointment.status == APPT.BOOKED,
            Appointment.start_at >= now_utc(),
            Appointment.superseded_at.is_(None),
        )
        .group_by(Appointment.calendar_id)
    )
    return dict(rows.tuples().all())


def build_calendar(calendar: BookingCalendar, upcoming: int) -> BookingCalendarOut:
    from app.core.read_models import region_ref

    return BookingCalendarOut(
        id=calendar.id,
        external_id=calendar.external_id,
        name=calendar.name,
        region=region_ref(calendar.region),
        service_ids=list(calendar.service_ids or []),
        calendar_type=calendar.calendar_type,
        is_enabled=calendar.is_enabled,
        synced_at=calendar.synced_at,
        upcoming_appointments=upcoming,
    )


async def build_appointments(
    session: AsyncSession, rows: Sequence[Appointment]
) -> list[AppointmentOut]:
    sr_ids = {r.service_request_id for r in rows if r.service_request_id is not None}
    srs: dict[int, tuple[str, SR]] = {}
    if sr_ids:
        result = await session.execute(
            select(ServiceRequest.id, ServiceRequest.number, ServiceRequest.status).where(
                ServiceRequest.id.in_(sr_ids)
            )
        )
        srs = {sr_id: (number, status) for sr_id, number, status in result.tuples()}
    out: list[AppointmentOut] = []
    for row in rows:
        sr = srs.get(row.service_request_id) if row.service_request_id is not None else None
        out.append(
            AppointmentOut(
                id=row.id,
                external_id=row.external_id,
                calendar=CalendarRef(id=row.calendar.id, name=row.calendar.name),
                service_request=AppointmentSrRef(
                    id=row.service_request_id, number=sr[0], status=sr[1]
                )
                if sr is not None and row.service_request_id is not None
                else None,
                reference=row.reference,
                status=row.status,
                start_at=row.start_at,
                end_at=row.end_at,
                title=row.title,
                contact_name=row.contact_name,
                contact_email=row.contact_email,
                street=row.street,
                town=row.town,
                notes=row.notes,
                booked_at=row.booked_at,
                source="manual" if row.source == "manual" else "sync",
                is_current=row.is_current,
            )
        )
    return out


# --- appointment rows -------------------------------------------------------------------------


def apply_provider_fields(row: Appointment, appt: ProviderAppointment, now: datetime) -> None:
    row.reference = appt.reference
    row.status = APPT(appt.status)
    row.start_at = appt.start_at
    row.end_at = appt.end_at
    row.title = appt.title
    row.contact_name = appt.contact_name
    row.contact_email = appt.contact_email
    row.street = appt.street
    row.town = appt.town
    row.notes = appt.notes
    if row.booked_at is None:
        row.booked_at = appt.created_at
    row.provider_updated_at = appt.updated_at
    row.last_synced_at = now


async def appointment_by_external(session: AsyncSession, external_id: str) -> Appointment | None:
    return (
        await session.scalars(select(Appointment).where(Appointment.external_id == external_id))
    ).first()


async def sr_by_number(session: AsyncSession, number: str) -> ServiceRequest | None:
    return (
        await session.scalars(
            select(ServiceRequest).where(ServiceRequest.number == number.strip().upper())
        )
    ).first()


async def last_reschedule_at(session: AsyncSession, sr_id: int) -> datetime | None:
    return await session.scalar(
        select(func.max(StatusTransition.occurred_at)).where(
            StatusTransition.entity_type == "service_request",
            StatusTransition.entity_id == sr_id,
            StatusTransition.to_status == SR.RESCHEDULE_NEEDED,
        )
    )


def _ranking(row: Appointment) -> tuple[datetime, datetime]:
    return (row.booked_at or row.created_at or row.start_at, row.start_at)


# --- reconciliation (§6.18 step 4–5) ----------------------------------------------------------


async def reconcile_sr(
    session: AsyncSession,
    sr: ServiceRequest,
    *,
    now: datetime | None = None,
    actor: User | None = None,
) -> int:
    """Apply the appointment rows of ``sr`` to its status. Returns transitions applied."""
    now = now or now_utc()
    if sr.status in SR_TERMINAL or sr.status == SR.HARDWARE_UNAVAILABLE:
        return 0
    rows = list(
        (
            await session.scalars(
                select(Appointment).where(Appointment.service_request_id == sr.id)
            )
        ).all()
    )
    current = next((r for r in rows if r.is_current), None)
    rescheduled_at = await last_reschedule_at(session, sr.id)

    def usable(row: Appointment) -> bool:
        if row.status != APPT.BOOKED or row.superseded_at is not None:
            return False
        if row is current and sr.status in WITH_APPT:
            return True
        if row.start_at <= now:
            return False  # SPEC_RESOLUTIONS #7: ignore past appointments
        booked = row.booked_at or row.created_at
        return rescheduled_at is None or booked is None or booked > rescheduled_at

    candidates = [r for r in rows if usable(r)]
    best = max(candidates, key=_ranking) if candidates else None

    if sr.status in PRE_APPT:
        if best is None:
            return 0
        _make_current(rows, best)
        _set_from(session, sr, best)
        await transition_service.apply(
            session,
            sr,
            "appointment_synced",
            actor=actor,
            trigger=Trigger.SYNC,
            payload={"appointmentId": best.id, "externalId": best.external_id},
        )
        sr.resume_status = None
        await drop_moved_stop(session, sr)
        return 1

    if sr.status not in WITH_APPT:
        return 0
    if best is None:
        if current is None or current.status != APPT.CANCELLED:
            return 0
        current.is_current = False
        from_status = sr.status
        await transition_service.apply(
            session,
            sr,
            "appointment_cancelled",
            actor=actor,
            trigger=Trigger.SYNC,
            payload={"appointmentId": current.id, "externalId": current.external_id},
        )
        await reschedule_effects(session, sr, from_status, actor=actor)
        return 1
    if best is not current:
        _make_current(rows, best)
    if (
        sr.appointment_start_at != best.start_at
        or sr.appointment_end_at != best.end_at
        or sr.appointment_calendar_id != best.calendar_id
    ):
        _set_from(session, sr, best)
        await drop_moved_stop(session, sr)
    return 0


def _make_current(rows: Iterable[Appointment], best: Appointment) -> None:
    for row in rows:
        row.is_current = row is best


def _set_from(session: AsyncSession, sr: ServiceRequest, row: Appointment) -> None:
    set_appointment(
        session,
        sr,
        start_at=row.start_at,
        end_at=row.end_at,
        calendar_id=row.calendar_id,
        calendar_name=row.calendar.name if row.calendar is not None else None,
    )


# --- sync (§6.18) -----------------------------------------------------------------------------


@dataclass
class SyncRun:
    counts: SyncCounts = field(default_factory=SyncCounts)
    affected: set[int] = field(default_factory=set)
    seen: set[str] = field(default_factory=set)


async def get_state(session: AsyncSession) -> BookingSyncState:
    state = await session.get(BookingSyncState, 1)
    if state is None:
        state = BookingSyncState(id=1)
        session.add(state)
        await session.flush()
    return state


async def sync_calendars(
    session: AsyncSession, provider: BookingProvider, now: datetime
) -> dict[str, BookingCalendar]:
    """Upsert provider calendars; new ones are enabled only for ``cal-<region code>``."""
    existing = {c.external_id: c for c in (await session.scalars(select(BookingCalendar))).all()}
    regions = {r.code: r for r in (await session.scalars(select(Region))).all()}
    for remote in await provider.list_calendars():
        calendar = existing.get(remote.external_id)
        try:
            service_ids = [
                s.external_id for s in await provider.list_calendar_services(remote.external_id)
            ]
        except BookingProviderError:  # pragma: no cover - provider hiccup: keep previous ids
            log.warning("calendar_services_failed", calendar=remote.external_id)
            service_ids = list(calendar.service_ids) if calendar else [BOOKING_SERVICE_ID]
        if calendar is None:
            match = _CAL_RE.match(remote.external_id)
            region = regions.get(match.group(1)) if match else None
            calendar = BookingCalendar(
                external_id=remote.external_id,
                name=remote.name,
                region_id=region.id if region else None,
                is_enabled=region is not None and remote.enabled,
            )
            session.add(calendar)
            existing[remote.external_id] = calendar
        calendar.calendar_type = remote.calendar_type
        calendar.service_ids = service_ids
        calendar.synced_at = now
    await session.flush()
    return existing


async def upsert_appointment(
    session: AsyncSession,
    appt: ProviderAppointment,
    calendars: dict[str, BookingCalendar],
    run: SyncRun,
    now: datetime,
) -> Appointment | None:
    """Upsert one provider appointment by ``external_id`` and match it to an SR (§6.18 step 3)."""
    from .common import match_sr_number

    calendar = calendars.get(appt.calendar_external_id)
    if calendar is None or not calendar.is_enabled:
        return None
    run.seen.add(appt.external_id)
    row = await appointment_by_external(session, appt.external_id)
    was_status = row.status if row is not None else None
    if row is None:
        row = Appointment(external_id=appt.external_id, calendar=calendar, source="sync")
        session.add(row)
        if appt.status == "booked":
            run.counts.created += 1
    elif (
        row.start_at != appt.start_at
        or row.end_at != appt.end_at
        or str(row.status) != appt.status
        or row.calendar_id != calendar.id
    ):
        if appt.status == "booked":
            run.counts.updated += 1
    if appt.status == "cancelled" and was_status != APPT.CANCELLED:
        run.counts.cancelled += 1
    row.calendar = calendar
    row.calendar_id = calendar.id
    apply_provider_fields(row, appt, now)
    if row.service_request_id is None:
        number = match_sr_number(appt.reference, appt.title, appt.notes)
        sr = await sr_by_number(session, number) if number else None
        if sr is not None:
            row.service_request_id = sr.id
            run.counts.matched += 1
        elif was_status is None:
            run.counts.unmatched += 1
    if row.service_request_id is not None:
        run.affected.add(row.service_request_id)
    return row


async def run_sync(
    session: AsyncSession,
    provider: BookingProvider,
    *,
    now: datetime | None = None,
    job_id: int | None = None,
) -> SyncCounts:
    """One ``appointments.sync`` run (caller commits)."""
    now = now or now_utc()
    state = await get_state(session)
    run = SyncRun()
    calendars = await sync_calendars(session, provider, now)
    run.counts.calendars = len(calendars)

    changes = (
        None if state.sync_token is None else await provider.sync_appointments(state.sync_token)
    )
    if changes is None or changes.full_resync_required:
        baseline = await provider.sync_appointments(None)
        since = business_today(now) - timedelta(days=FULL_SYNC_DAYS)
        processed = 0
        for calendar in calendars.values():
            if not calendar.is_enabled:
                continue
            async for appt in provider.list_appointments(calendar.external_id, since=since):
                await upsert_appointment(session, appt, calendars, run, now)
                processed += 1
                if processed % PAGE == 0:
                    await session.flush()
        for change in baseline.changes:
            appt = change.appointment
            if appt.external_id in run.seen or appt.start_at.date() < since:
                continue
            await upsert_appointment(session, appt, calendars, run, now)
        state.sync_token = baseline.next_sync_token
        state.last_full_sync_at = now
    else:
        for index, change in enumerate(changes.changes, start=1):
            await upsert_appointment(session, change.appointment, calendars, run, now)
            if index % PAGE == 0:
                await session.flush()
        state.sync_token = changes.next_sync_token
    state.last_incremental_sync_at = now
    await session.flush()

    for sr_id in sorted(run.affected):
        sr = await session.get(ServiceRequest, sr_id)
        if sr is None:
            continue
        run.counts.transitions += await reconcile_sr(session, sr, now=now)
    result: dict[str, Any] = run.counts.model_dump(mode="json", by_alias=True)
    state.last_result = result
    if job_id is not None:
        state.last_job_id = job_id
    await session.flush()
    log.info("appointments_synced", **result)
    return run.counts


async def link_appointment(
    session: AsyncSession, row: Appointment, sr: ServiceRequest, actor: User
) -> int:
    """``POST /appointments/{id}/link`` — link an unmatched appointment, then step 5."""
    row.service_request_id = sr.id
    row.reference = row.reference or sr.number
    await session.flush()
    return await reconcile_sr(session, sr, actor=actor)


async def sync_running(session: AsyncSession) -> BackgroundJob | None:
    return (
        await session.scalars(
            select(BackgroundJob)
            .where(
                BackgroundJob.kind == JobKind.APPOINTMENTS_SYNC,
                BackgroundJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
                BackgroundJob.created_at > now_utc() - timedelta(minutes=30),
            )
            .order_by(BackgroundJob.id.desc())
        )
    ).first()


def next_scheduled_at(now: datetime | None = None) -> datetime:
    """Next hourly run at minute :05 UTC (SPEC §9)."""
    now = now or now_utc()
    candidate = now.replace(minute=5, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(hours=1)
    return candidate


def counts_from(raw: dict[str, Any] | None) -> SyncCounts | None:
    if not raw:
        return None
    return SyncCounts.model_validate(raw)
