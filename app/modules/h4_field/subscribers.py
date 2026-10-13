"""Reactions to other slices' transitions (SPEC §4.1.1 #30 "H4 subscriber")."""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gateways
from app.core.db import after_commit, commit, open_session
from app.core.enums import AppointmentStatus, TourStopOutcome
from app.core.events import Transitioned
from app.core.gateways.protocols import BookingProviderError
from app.core.timeutil import now_utc

from .common import current_appointment
from .models import Appointment
from .tours import release_pending_stop

log = structlog.get_logger(__name__)


async def on_sr_transition(session: AsyncSession, event: Transitioned) -> None:
    """SR ``cancel`` (dispatcher or import): the pending stop is deleted — or marked
    ``cancelled`` in an ``in_progress`` tour (#8) — and the future provider booking is
    cancelled after commit (best effort)."""
    if event.entity_type != "service_request" or event.action != "cancel":
        return
    await release_pending_stop(session, event.entity_id, TourStopOutcome.CANCELLED)
    row = await current_appointment(session, event.entity_id)
    if row is None or row.status != AppointmentStatus.BOOKED or row.start_at <= now_utc():
        return
    row.is_current = False
    appointment_id, external_id = row.id, row.external_id

    async def cancel_at_provider() -> None:
        await _cancel_at_provider(appointment_id, external_id)

    after_commit(session, cancel_at_provider)


async def _cancel_at_provider(appointment_id: int, external_id: str) -> None:
    try:
        await gateways.booking().cancel_appointment(external_id, reason="Service request cancelled")
    except BookingProviderError:
        log.warning("provider_cancel_failed", appointment_id=appointment_id)
        return
    async with open_session() as session:
        row = await session.get(Appointment, appointment_id)
        if row is not None:
            row.status = AppointmentStatus.CANCELLED
            await commit(session)
