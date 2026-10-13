"""Shared effects of SR #15/#16/#27 (``appointment_cancelled``, ``request_reschedule``,
``reschedule_on_site``): SPEC §4.1.1, SPEC_RESOLUTIONS #7/#8."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assets import asset_service
from app.core.enums import (
    AppointmentStatus,
    AssetAction,
    AssetStatus,
    ServiceRequestStatus,
    TourStopOutcome,
    Trigger,
)
from app.core.loading import ensure_loaded
from app.core.models import ServiceRequest, User
from app.core.services import clear_appointment
from app.core.timeutil import now_utc

from .common import current_appointment
from .models import Appointment
from .tours import release_pending_stop

SR = ServiceRequestStatus


async def reschedule_effects(
    session: AsyncSession,
    sr: ServiceRequest,
    from_status: str,
    *,
    actor: User | None,
) -> None:
    """``resume_status`` = previous status, except ``provided`` → ``ready_for_picking`` with
    the new assets ``return_to_hub``; clear ``appointment_*``; pending stop rescheduled
    (``in_progress`` tour) or deleted."""
    await ensure_loaded(session, sr)
    if from_status == SR.PROVIDED:
        sr.resume_status = SR.READY_FOR_PICKING
        for item in sr.new_items:
            asset = item.asset
            if asset is not None and asset.status == AssetStatus.PROVIDED:
                await asset_service.move(
                    session,
                    asset,
                    AssetAction.RETURN_TO_HUB,
                    actor=actor,
                    trigger=Trigger.SYSTEM,
                    service_request=sr,
                )
    else:
        sr.resume_status = SR(from_status)
    clear_appointment(session, sr)
    await supersede_all(session, sr.id)
    await release_pending_stop(session, sr.id, TourStopOutcome.RESCHEDULED)


async def supersede_all(session: AsyncSession, sr_id: int) -> None:
    """SPEC_RESOLUTIONS #7: bookings that exist when the SR is rescheduled never become
    current again (the sync ignores superseded appointments)."""
    rows = (
        await session.scalars(
            select(Appointment).where(
                Appointment.service_request_id == sr_id,
                Appointment.status == AppointmentStatus.BOOKED,
                Appointment.superseded_at.is_(None),
            )
        )
    ).all()
    now = now_utc()
    for row in rows:
        row.is_current = False
        row.superseded_at = now


async def supersede_current(session: AsyncSession, sr_id: int) -> None:
    """SPEC_RESOLUTIONS #7: the provider booking stays, the local one is no longer current."""
    row = await current_appointment(session, sr_id)
    if row is not None:
        row.is_current = False
        row.superseded_at = now_utc()
