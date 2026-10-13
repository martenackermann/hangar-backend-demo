"""FakeBookingProvider (default binding, SPEC §8.1).

State lives in ``booking_sandbox_appointments`` so API and worker share it. Each call uses
its own session (the provider is "external": its writes commit independently of the
caller's transaction). Tests pass ``session_factory=shared_session(db_session)``.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.constants import BOOKING_CALENDARS, BOOKING_SERVICE_ID, calendar_for_region
from app.core.db import open_session
from app.core.enums import AppointmentStatus, ServiceRequestStatus
from app.core.gateways.protocols import (
    AppointmentChange,
    AppointmentChanges,
    BookingProviderError,
    CreateAppointmentRequest,
    ProviderAppointment,
    ProviderCalendar,
    ProviderService,
)
from app.core.models import BOOKING_SANDBOX_SEQ, BookingSandboxAppointment, Region, ServiceRequest
from app.core.timeutil import add_working_days, business_datetime, business_today, now_utc

SR = ServiceRequestStatus
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_SIMULATE_STATUSES = (
    SR.INVITED,
    SR.REMINDER_1_SENT,
    SR.REMINDER_2_SENT,
    SR.ESCALATED,
    SR.CAMPUS_INFO_SENT,
    SR.RESCHEDULE_NEEDED,
)
_THRESHOLD = {SR.RESCHEDULE_NEEDED: 128, SR.ESCALATED: 153}
_DEFAULT_THRESHOLD = 90
_FIRST_HOUR, _LAST_HOUR = 8, 15
PAGE = 200


def shared_session(session: AsyncSession) -> SessionFactory:
    """Session factory that reuses ``session`` (no commit/close) — for tests and seeding."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


def _own_session() -> AbstractAsyncContextManager[AsyncSession]:
    return open_session()


def to_provider(row: BookingSandboxAppointment) -> ProviderAppointment:
    return ProviderAppointment(
        external_id=row.external_id,
        calendar_external_id=row.calendar_external_id,
        start_at=row.start_at,
        end_at=row.end_at,
        status="cancelled" if row.status == AppointmentStatus.CANCELLED else "booked",
        reference=row.reference,
        title=f"Hardware rollout visit {row.reference}"
        if row.reference
        else "Hardware rollout visit",
        contact_name=row.contact_name,
        contact_email=row.contact_email,
        street=row.street,
        town=row.town,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def appointment_external_id(reference: str, start_at: datetime) -> str:
    digest = hashlib.sha256(f"{reference}{start_at.isoformat()}".encode()).hexdigest()
    return f"appt-{digest[:8]}"


class FakeBookingProvider:
    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._sessions = session_factory or _own_session
        self._clock = clock
        self._shared = session_factory is not None

    async def _commit(self, session: AsyncSession) -> None:
        if self._shared:
            await session.flush()
        else:
            await session.commit()

    # --- protocol --------------------------------------------------------------------------

    async def list_calendars(self) -> list[ProviderCalendar]:
        return [ProviderCalendar(c.external_id, c.name, True, "route") for c in BOOKING_CALENDARS]

    async def list_calendar_services(self, calendar_external_id: str) -> list[ProviderService]:
        return [ProviderService(BOOKING_SERVICE_ID, "Hardware rollout visit", 60)]

    async def list_appointments(
        self, calendar_external_id: str, *, since: date, until: date | None = None
    ) -> AsyncIterator[ProviderAppointment]:
        offset = 0
        while True:
            async with self._sessions() as session:
                stmt = (
                    select(BookingSandboxAppointment)
                    .where(BookingSandboxAppointment.calendar_external_id == calendar_external_id)
                    .where(BookingSandboxAppointment.start_at >= business_datetime(since))
                    .order_by(BookingSandboxAppointment.start_at, BookingSandboxAppointment.id)
                    .limit(PAGE)
                    .offset(offset)
                )
                if until is not None:
                    stmt = stmt.where(
                        BookingSandboxAppointment.start_at
                        < business_datetime(until + timedelta(days=1))
                    )
                rows = list((await session.scalars(stmt)).all())
            for row in rows:
                yield to_provider(row)
            if len(rows) < PAGE:
                return
            offset += PAGE

    async def sync_appointments(self, sync_token: str | None) -> AppointmentChanges:
        try:
            since = int(sync_token or 0)
        except ValueError:
            return AppointmentChanges([], "0", True)
        async with self._sessions() as session:
            await self.simulate_bookings(session, self._clock())
            await self._commit(session)
            rows = (
                await session.scalars(
                    select(BookingSandboxAppointment)
                    .where(BookingSandboxAppointment.change_seq > since)
                    .order_by(BookingSandboxAppointment.change_seq)
                )
            ).all()
            changes = [AppointmentChange(self._kind(row), to_provider(row)) for row in rows]  # pyright: ignore[reportArgumentType]
            token = max([since, *(row.change_seq for row in rows)])
        return AppointmentChanges(changes, str(token), False)

    async def create_appointment(self, request: CreateAppointmentRequest) -> ProviderAppointment:
        if request.end_at <= request.start_at:
            raise BookingProviderError("The appointment must end after it starts.")
        external_id = appointment_external_id(request.reference, request.start_at)
        async with self._sessions() as session:
            row = (
                await session.scalars(
                    select(BookingSandboxAppointment).where(
                        BookingSandboxAppointment.external_id == external_id
                    )
                )
            ).first()
            if row is None:
                row = BookingSandboxAppointment(external_id=external_id)
                session.add(row)
            row.calendar_external_id = request.calendar_external_id
            row.reference = request.reference
            row.status = AppointmentStatus.BOOKED
            row.start_at = request.start_at
            row.end_at = request.end_at
            row.contact_name = request.contact_name
            row.contact_email = request.contact_email
            row.street = request.street
            row.town = request.town
            row.notes = request.notes
            row.change_seq = BOOKING_SANDBOX_SEQ.next_value()
            await session.flush()
            await session.refresh(row)
            result = to_provider(row)
            await self._commit(session)
        return result

    async def cancel_appointment(self, external_id: str, *, reason: str | None = None) -> None:
        async with self._sessions() as session:
            row = (
                await session.scalars(
                    select(BookingSandboxAppointment).where(
                        BookingSandboxAppointment.external_id == external_id
                    )
                )
            ).first()
            if row is None:
                raise BookingProviderError(f"Unknown appointment {external_id}.")
            row.status = AppointmentStatus.CANCELLED
            if reason:
                row.notes = reason
            row.change_seq = BOOKING_SANDBOX_SEQ.next_value()
            await self._commit(session)

    def build_booking_link(self, *, destination_id: str, service_id: str, reference: str) -> str:
        base = get_settings().booking_base_url.rstrip("/")
        return f"{base}/book/{destination_id}/{service_id}?ref={reference}"

    # --- demo realism ------------------------------------------------------------------------

    @staticmethod
    def _kind(row: BookingSandboxAppointment) -> str:
        if row.status == AppointmentStatus.CANCELLED:
            return "cancelled"
        if abs((row.updated_at - row.created_at).total_seconds()) < 1:
            return "created"
        return "updated"

    async def simulate_bookings(self, session: AsyncSession, now: datetime) -> int:
        """Deterministic per SR and hour (SPEC §8.1). Returns the number of changes."""
        hour = now.strftime("%Y-%m-%dT%H")
        today = business_today(now)
        booked_rows = (
            await session.scalars(
                select(BookingSandboxAppointment).where(
                    BookingSandboxAppointment.status == AppointmentStatus.BOOKED
                )
            )
        ).all()
        future_refs = {r.reference for r in booked_rows if r.start_at > now and r.reference}
        taken = {(r.calendar_external_id, r.start_at) for r in booked_rows}

        rows = (
            await session.execute(
                select(ServiceRequest, Region.code)
                .join(Region, ServiceRequest.region_id == Region.id)
                .where(ServiceRequest.status.in_(_SIMULATE_STATUSES))
                .order_by(ServiceRequest.id)
            )
        ).all()
        changes = 0
        for sr, region_code in rows:
            if sr.number in future_refs:
                continue
            digest = hashlib.sha256(f"{sr.number}:{hour}".encode()).digest()
            if digest[0] >= _THRESHOLD.get(sr.status, _DEFAULT_THRESHOLD):
                continue
            calendar = calendar_for_region(region_code).external_id
            start = self._free_slot(
                taken,
                calendar,
                add_working_days(today, 1 + digest[1] % 15),
                _FIRST_HOUR + digest[2] % 8,
            )
            taken.add((calendar, start))
            contact = sr.contact
            session.add(
                BookingSandboxAppointment(
                    external_id=appointment_external_id(sr.number, start),
                    calendar_external_id=calendar,
                    reference=sr.number,
                    status=AppointmentStatus.BOOKED,
                    start_at=start,
                    end_at=start + timedelta(minutes=60),
                    contact_name=contact.display_name if contact else None,
                    contact_email=contact.email if contact else None,
                    street=sr.delivery_street,
                    town=sr.delivery_city,
                )
            )
            changes += 1

        run = hashlib.sha256(hour.encode()).digest()
        if run[0] < 26:
            horizon = business_datetime(add_working_days(today, 2))
            candidates = (
                await session.scalars(
                    select(BookingSandboxAppointment)
                    .join(
                        ServiceRequest, ServiceRequest.number == BookingSandboxAppointment.reference
                    )
                    .where(BookingSandboxAppointment.status == AppointmentStatus.BOOKED)
                    .where(BookingSandboxAppointment.start_at >= horizon)
                    .where(ServiceRequest.status == SR.APPOINTMENT_BOOKED)
                    .order_by(BookingSandboxAppointment.external_id)
                )
            ).all()
            if candidates:
                victim = candidates[run[1] % len(candidates)]
                victim.status = AppointmentStatus.CANCELLED
                victim.notes = "Cancelled by the user in the booking portal (demo simulation)"
                victim.change_seq = BOOKING_SANDBOX_SEQ.next_value()
                changes += 1
        await session.flush()
        return changes

    @staticmethod
    def _free_slot(
        taken: set[tuple[str, datetime]], calendar: str, day: date, hour: int
    ) -> datetime:
        while True:
            for h in range(hour, _LAST_HOUR + 1):
                start = business_datetime(day, h)
                if (calendar, start) not in taken:
                    return start
            day = add_working_days(day, 1)
            hour = _FIRST_HOUR

    async def mirror(
        self, session: AsyncSession, appointments: Iterable[ProviderAppointment]
    ) -> int:
        """Seeding helper: write sandbox rows mirroring ``appointments``; returns max change_seq."""
        for appt in appointments:
            session.add(
                BookingSandboxAppointment(
                    external_id=appt.external_id,
                    calendar_external_id=appt.calendar_external_id,
                    reference=appt.reference,
                    status=AppointmentStatus(appt.status),
                    start_at=appt.start_at,
                    end_at=appt.end_at,
                    contact_name=appt.contact_name,
                    contact_email=appt.contact_email,
                    street=appt.street,
                    town=appt.town,
                    notes=appt.notes,
                )
            )
        await session.flush()
        return int(
            (
                await session.execute(
                    select(func.coalesce(func.max(BookingSandboxAppointment.change_seq), 0))
                )
            ).scalar_one()
        )
