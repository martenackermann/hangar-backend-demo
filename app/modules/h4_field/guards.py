"""H4 guards and the ``own_tour`` authorizer (SPEC_RESOLUTIONS #2, #3, #33).

Guard codes: ``calendar_enabled``, ``deployment_scans_complete``, ``deviation_outcome``,
``tour_startable``. Guards have no side effects (they also compute ``blockedActions``).
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import can
from app.core.enums import DeviationOutcome, TourStatus
from app.core.models import DeviationCode, ServiceRequest, User
from app.core.statemachine import GuardFailure, GuardFn
from app.core.timeutil import business_today

from .common import (
    GUARD_CALENDAR_ENABLED,
    GUARD_DEVIATION_OUTCOME,
    GUARD_SCANS_COMPLETE,
    GUARD_TOUR_STARTABLE,
    counted_items,
    is_new_slot,
    pending_stop,
)
from .models import BookingCalendar

#: Which deviation outcome each on-site action accepts.
ACTION_OUTCOME: dict[str, DeviationOutcome] = {
    "complete_deployment": DeviationOutcome.COMPLETE,
    "reschedule_on_site": DeviationOutcome.RESCHEDULE,
    "cancel_on_site": DeviationOutcome.CANCEL,
}


async def calendar_enabled(
    session: AsyncSession, _sr: ServiceRequest, _actor: User | None, payload: BaseModel | None
) -> GuardFailure | None:
    calendar_id = getattr(payload, "calendar_id", None)
    if calendar_id is None:
        return None
    calendar = await session.get(BookingCalendar, calendar_id)
    if calendar is None or not calendar.is_enabled:
        return GuardFailure(
            GUARD_CALENDAR_ENABLED, "This booking calendar is disabled and cannot be booked."
        )
    return None


async def _deviation(session: AsyncSession, payload: BaseModel | None) -> DeviationCode | None:
    code_id = getattr(payload, "deviation_code_id", None)
    if code_id is None:
        return None
    return await session.get(DeviationCode, code_id)


async def deployment_scans_complete(
    session: AsyncSession, sr: ServiceRequest, _actor: User | None, payload: BaseModel | None
) -> GuardFailure | None:
    """SPEC §6.11: every new item with an asset and every old item verified, or a deviation
    with outcome ``complete``; ``handoverConfirmed`` when a new item was verified."""
    items = counted_items(sr)
    open_items = [i for i in items if i.verified_at is None]
    if open_items:
        code = await _deviation(session, payload)
        if code is None or code.outcome != DeviationOutcome.COMPLETE:
            return GuardFailure(
                GUARD_SCANS_COMPLETE,
                f"{len(open_items)} of {len(items)} devices are not verified yet. "
                "Scan them or choose a deviation.",
            )
    if payload is not None:
        verified_new = any(is_new_slot(i.slot) and i.verified_at is not None for i in sr.items)
        if verified_new and not getattr(payload, "handover_confirmed", False):
            return GuardFailure(
                GUARD_SCANS_COMPLETE, "Confirm that the recipient received the hardware."
            )
    return None


def deviation_outcome(expected: DeviationOutcome) -> GuardFn:
    """The deviation code's outcome must match the on-site action (one guard per action)."""

    async def guard(
        session: AsyncSession, _sr: ServiceRequest, _actor: User | None, payload: BaseModel | None
    ) -> GuardFailure | None:
        code = await _deviation(session, payload)
        if code is None:
            return None
        if code.outcome != expected:
            return GuardFailure(
                GUARD_DEVIATION_OUTCOME,
                f"Deviation {code.code} is for outcome '{code.outcome}', not '{expected}'.",
            )
        return None

    guard.__name__ = f"deviation_outcome_{expected}"
    return guard


async def tour_startable(
    session: AsyncSession, sr: ServiceRequest, _actor: User | None, _payload: BaseModel | None
) -> GuardFailure | None:
    """SPEC_RESOLUTIONS #32: on-site actions need the stop's tour on the road (or startable)."""
    stop = await pending_stop(session, sr.id)
    if stop is None:
        return None
    tour = stop.tour
    if tour.status == TourStatus.IN_PROGRESS:
        return None
    if (
        tour.status == TourStatus.PLANNED
        and tour.tour_date == business_today()
        and tour.technician_id is not None
    ):
        return None
    return GuardFailure(
        GUARD_TOUR_STARTABLE,
        f"The tour of this stop is not on the road (tour date {tour.tour_date.isoformat()}, "
        f"status {tour.status}).",
    )


async def own_tour(session: AsyncSession, sr: ServiceRequest, actor: User) -> bool:
    """SR #26–#28: ``tours.execute_any``, or ``tours.execute_own`` on the own pending stop."""
    if can(actor, "tours.execute_any"):
        return True
    if not can(actor, "tours.execute_own"):
        return False
    stop = await pending_stop(session, sr.id)
    return stop is not None and stop.tour.technician_id == actor.id
