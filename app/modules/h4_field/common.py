"""Small shared helpers of the H4 slice (no HTTP, no commits)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import can
from app.core.enums import (
    ItemSlot,
    Readiness,
    ServiceRequestStatus,
    TourStatus,
    TourStopOutcome,
)
from app.core.models import ServiceRequest, ServiceRequestItem, User
from app.core.timeutil import business_datetime

from .models import Appointment, Tour, TourStop

SR = ServiceRequestStatus

#: Guard / problem codes owned by H4 (SPEC_RESOLUTIONS #33).
GUARD_CALENDAR_ENABLED = "calendar_enabled"
GUARD_SCANS_COMPLETE = "deployment_scans_complete"
GUARD_DEVIATION_OUTCOME = "deviation_outcome"
GUARD_TOUR_STARTABLE = "tour_startable"
GUARD_TOUR_DATE_TODAY = "tour_date_today"
GUARD_NO_PENDING_STOPS = "no_pending_stops"
NOT_OWN_TOUR = "not_own_tour"

#: SR statuses in which the hardware is with the technician / the user (deployment possible).
ON_SITE_STATUSES = frozenset({SR.PROVIDED, SR.SHIPPED})

_READINESS: dict[str, Readiness] = {
    SR.PROVIDED: Readiness.READY,
    SR.SHIPPED: Readiness.READY,
    SR.COMPLETED: Readiness.READY,
    SR.READY_FOR_PICKING: Readiness.TO_PICK,
    SR.PERSONALIZATION_IN_PROGRESS: Readiness.IN_STAGING,
    SR.PERSONALIZATION_DONE: Readiness.IN_STAGING,
}

SR_NUMBER_RE = re.compile(r"(?<![A-Z0-9])SR[0-9]{7}(?![0-9])", re.IGNORECASE)


def readiness_of(status: str) -> Readiness:
    """SPEC §4.10 ``Readiness`` for a stop / candidate."""
    return _READINESS.get(status, Readiness.NOT_STARTED)


def match_sr_number(*texts: str | None) -> str | None:
    """First ``SR`` + 7 digits found in the given texts (reference, title, notes — §6.18)."""
    for value in texts:
        if not value:
            continue
        found = SR_NUMBER_RE.search(value)
        if found:
            return found.group(0).upper()
    return None


def normalize_code(value: str | None) -> str:
    """Scanned serials / asset tags: trimmed and upper-cased (SPEC §0)."""
    return (value or "").strip().upper()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC bounds ``[start, end)`` of a business day."""
    return business_datetime(day), business_datetime(day + timedelta(days=1))


def can_execute_tour(user: User | None, tour: Tour | None) -> bool:
    """``tours.execute_any`` or ``tours.execute_own`` on the user's own tour (SPEC §3.2)."""
    if user is None:
        return False
    if can(user, "tours.execute_any"):
        return True
    return tour is not None and tour.technician_id == user.id and can(user, "tours.execute_own")


async def pending_stop(session: AsyncSession, sr_id: int) -> TourStop | None:
    """The SR's pending stop (at most one, partial unique index)."""
    return (
        await session.scalars(
            select(TourStop).where(
                TourStop.service_request_id == sr_id,
                TourStop.outcome == TourStopOutcome.PENDING,
            )
        )
    ).first()


async def latest_stop(session: AsyncSession, sr_id: int) -> TourStop | None:
    """Pending stop, else the most recent stop of any outcome."""
    stop = await pending_stop(session, sr_id)
    if stop is not None:
        return stop
    return (
        await session.scalars(
            select(TourStop)
            .join(Tour, Tour.id == TourStop.tour_id)
            .where(TourStop.service_request_id == sr_id)
            .order_by(Tour.tour_date.desc(), TourStop.id.desc())
        )
    ).first()


async def current_appointment(session: AsyncSession, sr_id: int) -> Appointment | None:
    return (
        await session.scalars(
            select(Appointment)
            .where(Appointment.service_request_id == sr_id, Appointment.is_current.is_(True))
            .order_by(Appointment.id.desc())
        )
    ).first()


def item_label(item: ServiceRequestItem) -> str:
    """Stop item label: article name / requested type (new), asset tag (old)."""
    if item.slot in (ItemSlot.NEW_BASE, ItemSlot.NEW_PERIPHERAL_1, ItemSlot.NEW_PERIPHERAL_2):
        if item.article is not None:
            return item.article.name
        return item.requested_type or "Device"
    if item.asset is not None and item.asset.asset_tag:
        return item.asset.asset_tag
    return item.expected_asset_tag or (item.asset.serial_number if item.asset else "Device")


def is_new_slot(slot: str) -> bool:
    return str(slot).startswith("new_")


def counted_items(sr: ServiceRequest) -> list[ServiceRequestItem]:
    """Items a deployment verifies: new items with an asset, every old item."""
    return [i for i in sr.items if (is_new_slot(i.slot) and i.asset_id is not None)] + [
        i for i in sr.items if not is_new_slot(i.slot)
    ]


def tour_is_open(tour: Tour) -> bool:
    return tour.status in (TourStatus.DRAFT, TourStatus.PLANNED, TourStatus.IN_PROGRESS)
