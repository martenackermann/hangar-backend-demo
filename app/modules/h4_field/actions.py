"""Payloads and handlers of the H4 SR actions (SPEC §4.1.1 #13, #16, #26–#29; API.md §3.8).

Handlers run the transition first (permission, authorizers, guards), then the effects; the
generic endpoint commits and returns the refreshed SR detail.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import AwareDatetime, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gateways
from app.core.actions import ActionBody
from app.core.constants import BOOKING_SERVICE_ID
from app.core.enums import AppointmentStatus, Trigger
from app.core.errors import GatewayError, ValidationProblem
from app.core.gateways.protocols import BookingProviderError, CreateAppointmentRequest
from app.core.loading import ensure_loaded
from app.core.models import DeviationCode, ServiceRequest, User
from app.core.services import set_appointment
from app.core.timeutil import now_utc
from app.core.transitions import transition_service

from .appointments import apply_provider_fields, appointment_by_external
from .common import current_appointment, pending_stop
from .deployment import cancel_on_site_effects, completion_effects
from .models import Appointment, BookingCalendar
from .protocol import generate_protocol, send_protocol
from .reschedule import reschedule_effects, supersede_current
from .tours import ensure_running


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("Must not be empty.")
    return value.strip()


# --- payloads ---------------------------------------------------------------------------------


class BookAppointmentBody(ActionBody):
    calendar_id: int
    start_at: AwareDatetime
    end_at: AwareDatetime | None = None


class RequestRescheduleBody(ActionBody):
    reason: str = Field(min_length=1, max_length=500)
    notify_user: bool = True
    cancel_at_provider: bool = True

    _reason = field_validator("reason")(_not_blank)


class CompleteDeploymentBody(ActionBody):
    recipient_name: str = Field(min_length=1, max_length=160)
    handover_confirmed: bool
    deviation_code_id: int | None = None
    deviation_comment: str | None = Field(default=None, max_length=2000)
    send_protocol: bool = True
    protocol_email: EmailStr | None = None

    _recipient = field_validator("recipient_name")(_not_blank)


class OnSiteDeviationBody(ActionBody):
    deviation_code_id: int
    deviation_comment: str | None = Field(default=None, max_length=2000)


# --- helpers ----------------------------------------------------------------------------------


def booking_error(exc: Exception) -> GatewayError:
    return GatewayError(
        f"The booking provider failed: {exc}", code="booking_error", title="Booking provider error"
    )


async def check_deviation(
    session: AsyncSession, code_id: int | None, comment: str | None
) -> DeviationCode | None:
    """Unknown/inactive code → 422 ``deviationCodeId``; missing comment → 422
    ``deviationComment`` (SPEC_RESOLUTIONS #34). Outcome mismatch is the guard's job."""
    if code_id is None:
        return None
    code = await session.get(DeviationCode, code_id)
    if code is None or not code.is_active:
        raise ValidationProblem({"deviationCodeId": ["Unknown or inactive deviation code."]})
    if code.requires_comment and not (comment or "").strip():
        raise ValidationProblem({"deviationComment": [f"{code.code} requires a comment."]})
    return code


def store_deviation(sr: ServiceRequest, code: DeviationCode | None, comment: str | None) -> None:
    sr.deviation_code = code
    sr.deviation_code_id = code.id if code is not None else None
    sr.deviation_comment = (comment or "").strip() or None


# --- #13 book_appointment ---------------------------------------------------------------------


async def book_appointment(
    session: AsyncSession, sr: ServiceRequest, actor: User, payload: ActionBody
) -> None:
    assert isinstance(payload, BookAppointmentBody)
    calendar = await session.get(BookingCalendar, payload.calendar_id)
    if calendar is None:
        raise ValidationProblem({"calendarId": ["Unknown booking calendar."]})
    end_at = payload.end_at or payload.start_at + timedelta(minutes=60)
    if end_at <= payload.start_at:
        raise ValidationProblem({"endAt": ["The appointment must end after it starts."]})
    await ensure_loaded(session, sr)
    await transition_service.apply(
        session,
        sr,
        "book_appointment",
        actor=actor,
        trigger=Trigger.USER,
        payload=payload,
        comment=payload.comment,
    )
    contact = sr.contact
    try:
        booked = await gateways.booking().create_appointment(
            CreateAppointmentRequest(
                calendar_external_id=calendar.external_id,
                service_id=(calendar.service_ids or [BOOKING_SERVICE_ID])[0],
                start_at=payload.start_at,
                end_at=end_at,
                reference=sr.number,
                contact_name=contact.display_name,
                contact_email=contact.email,
                street=sr.delivery_street,
                town=sr.delivery_city,
                notes=payload.comment,
            )
        )
    except BookingProviderError as exc:
        raise booking_error(exc) from exc
    for row in (
        await session.scalars(
            select(Appointment).where(
                Appointment.service_request_id == sr.id, Appointment.is_current.is_(True)
            )
        )
    ).all():
        row.is_current = False
    row = await appointment_by_external(session, booked.external_id)
    if row is None:
        row = Appointment(external_id=booked.external_id, source="manual")
        session.add(row)
    row.calendar = calendar
    row.calendar_id = calendar.id
    row.service_request_id = sr.id
    row.superseded_at = None
    apply_provider_fields(row, booked, now_utc())
    row.booked_at = booked.created_at
    row.is_current = True
    set_appointment(
        session,
        sr,
        start_at=booked.start_at,
        end_at=booked.end_at,
        calendar_id=calendar.id,
        calendar_name=calendar.name,
    )
    sr.resume_status = None
    await session.flush()


# --- #16 request_reschedule -------------------------------------------------------------------


async def request_reschedule(
    session: AsyncSession, sr: ServiceRequest, actor: User, payload: ActionBody
) -> None:
    assert isinstance(payload, RequestRescheduleBody)
    from_status = sr.status
    current = await current_appointment(session, sr.id)
    await transition_service.apply(
        session,
        sr,
        "request_reschedule",
        actor=actor,
        trigger=Trigger.USER,
        payload=payload,
        comment=payload.comment or payload.reason,
    )
    if current is not None:
        if payload.cancel_at_provider:
            try:
                await gateways.booking().cancel_appointment(
                    current.external_id, reason=payload.reason
                )
            except BookingProviderError as exc:
                raise booking_error(exc) from exc
            current.status = AppointmentStatus.CANCELLED
            current.is_current = False
        else:
            await supersede_current(session, sr.id)
    await reschedule_effects(session, sr, from_status, actor=actor)
    await session.flush()


# --- #26 complete_deployment ------------------------------------------------------------------


async def complete_deployment(
    session: AsyncSession, sr: ServiceRequest, actor: User, payload: ActionBody
) -> None:
    assert isinstance(payload, CompleteDeploymentBody)
    code = await check_deviation(session, payload.deviation_code_id, payload.deviation_comment)
    stop = await pending_stop(session, sr.id)
    await transition_service.apply(
        session,
        sr,
        "complete_deployment",
        actor=actor,
        trigger=Trigger.USER,
        payload=payload,
        comment=payload.deviation_comment or payload.comment,
    )
    if stop is not None:
        await ensure_running(session, stop.tour, actor)
    store_deviation(sr, code, payload.deviation_comment)
    await completion_effects(session, sr, actor, verified_only=True)
    await generate_protocol(session, sr, created_by=actor)
    if payload.send_protocol:
        email = payload.protocol_email or (sr.contact.email if sr.contact else None)
        if email:
            await send_protocol(session, sr, email=email, actor=actor)
    await session.flush()


# --- #27 reschedule_on_site / #28 cancel_on_site ----------------------------------------------


async def reschedule_on_site(
    session: AsyncSession, sr: ServiceRequest, actor: User, payload: ActionBody
) -> None:
    assert isinstance(payload, OnSiteDeviationBody)
    code = await check_deviation(session, payload.deviation_code_id, payload.deviation_comment)
    stop = await pending_stop(session, sr.id)
    from_status = sr.status
    await transition_service.apply(
        session,
        sr,
        "reschedule_on_site",
        actor=actor,
        trigger=Trigger.USER,
        payload=payload,
        comment=payload.deviation_comment or payload.comment,
    )
    if stop is not None:
        await ensure_running(session, stop.tour, actor)
    store_deviation(sr, code, payload.deviation_comment)
    await supersede_current(session, sr.id)
    await reschedule_effects(session, sr, from_status, actor=actor)
    await session.flush()


async def cancel_on_site(
    session: AsyncSession, sr: ServiceRequest, actor: User, payload: ActionBody
) -> None:
    assert isinstance(payload, OnSiteDeviationBody)
    code = await check_deviation(session, payload.deviation_code_id, payload.deviation_comment)
    stop = await pending_stop(session, sr.id)
    await transition_service.apply(
        session,
        sr,
        "cancel_on_site",
        actor=actor,
        trigger=Trigger.USER,
        payload=payload,
        comment=payload.deviation_comment or payload.comment,
    )
    if stop is not None:
        await ensure_running(session, stop.tour, actor)
    store_deviation(sr, code, payload.deviation_comment)
    await cancel_on_site_effects(session, sr, actor)


# --- #29 complete_manually --------------------------------------------------------------------


async def complete_manually(
    session: AsyncSession, sr: ServiceRequest, actor: User, payload: ActionBody
) -> None:
    await transition_service.apply(
        session,
        sr,
        "complete_manually",
        actor=actor,
        trigger=Trigger.USER,
        payload=payload,
        comment=payload.comment,
    )
    await completion_effects(session, sr, actor, verified_only=False)
