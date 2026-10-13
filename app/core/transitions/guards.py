"""Core guards — only those that read core columns (SPEC_RESOLUTIONS #3).

Slice-owned guards (registered via ``SliceRegistry.register_guard``):
H1 ``chain_due``, ``not_paused`` (job trigger), ``retirement_window``;
H4 ``calendar_enabled``, ``deployment_scans_complete``, ``deviation_outcome``.
Missing comments/reasons are request validation errors (422), not guards (#34).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import PARCEL_DELIVERY, DeliveryType, Role, ServiceRequestStatus, Trigger
from app.core.models import LeaseReturn, ServiceRequest, User
from app.core.rules import software_checklist
from app.core.statemachine import Guard, GuardFailure, guards
from app.core.timeutil import now_utc

SR = ServiceRequestStatus
UJ = frozenset({Trigger.USER, Trigger.JOB})


def _fail(code: str, message: str) -> GuardFailure:
    return GuardFailure(code, message)


async def not_campus(
    _s: AsyncSession, sr: ServiceRequest, _a: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    if sr.is_campus:
        return _fail(
            "not_campus", "Campus users get the campus info mail instead of a booking link."
        )
    return None


async def is_campus(
    _s: AsyncSession, sr: ServiceRequest, _a: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    if not sr.is_campus:
        return _fail("is_campus", "Only campus requests get the campus info mail.")
    return None


async def region_resolved(
    _s: AsyncSession, sr: ServiceRequest, _a: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    region = sr.region
    if region is None or not region.booking_destination_id or not region.booking_service_id:
        return _fail(
            "region_resolved", "No region with a booking link is resolved for this request."
        )
    return None


async def contact_email(
    _s: AsyncSession, entity: Any, _a: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    contact = entity.contact
    if contact is None or not contact.email:
        return _fail("contact_email", "The contact has no email address.")
    return None


async def start_in_future(
    _s: AsyncSession, _sr: ServiceRequest, _a: User | None, payload: BaseModel | None
) -> GuardFailure | None:
    start = getattr(payload, "start_at", None)
    if isinstance(start, datetime) and start <= now_utc():
        return _fail("start_in_future", "The appointment must start in the future.")
    return None


async def has_appointment(
    _s: AsyncSession, sr: ServiceRequest, _a: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    if sr.appointment_start_at is None:
        return _fail("has_appointment", "The request has no appointment.")
    return None


async def is_editor(
    _s: AsyncSession, sr: ServiceRequest, actor: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    if sr.status != SR.PERSONALIZATION_IN_PROGRESS:
        return None  # e.g. skip_preparation from appointment_booked
    if actor is None or sr.editor_user_id != actor.id:
        return _fail("is_editor", "Only the current editor can do this. Claim the request first.")
    return None


async def is_editor_or_admin(
    _s: AsyncSession, sr: ServiceRequest, actor: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    if actor is not None and (sr.editor_user_id == actor.id or actor.role == Role.ADMIN):
        return None
    return _fail(
        "is_editor_or_admin", "Only the current editor or an admin can release this request."
    )


async def all_new_items_assigned(
    _s: AsyncSession, sr: ServiceRequest, _a: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    if sr.status not in (SR.PERSONALIZATION_IN_PROGRESS, SR.PERSONALIZATION_DONE):
        return None
    missing = [item.slot for item in sr.new_items if item.asset_id is None]
    if missing:
        return _fail(
            "all_new_items_assigned", f"Not all new devices are assigned ({len(missing)} open)."
        )
    return None


async def software_checklist_complete(
    _s: AsyncSession, sr: ServiceRequest, _a: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    checklist = software_checklist(sr.requested_software, sr.added_software, sr.installed_software)
    if not checklist.complete:
        return _fail(
            "software_checklist_complete",
            f"Software checklist incomplete: {', '.join(checklist.missing)}.",
        )
    return None


async def delivery_type_is_technician(
    _s: AsyncSession, sr: ServiceRequest, _a: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    if sr.effective_delivery_type != DeliveryType.TECHNICIAN:
        return _fail("delivery_type_is_technician", "This request is delivered by parcel.")
    return None


async def delivery_type_is_parcel(
    _s: AsyncSession, sr: ServiceRequest, _a: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    if sr.effective_delivery_type not in PARCEL_DELIVERY:
        return _fail("delivery_type_is_parcel", "This request is delivered by a technician.")
    return None


async def all_new_items_packed(
    _s: AsyncSession, sr: ServiceRequest, _a: User | None, _p: BaseModel | None
) -> GuardFailure | None:
    open_items = [i for i in sr.new_items if i.packed_at is None]
    if open_items:
        return _fail(
            "all_new_items_packed", f"Not all new devices are packed ({len(open_items)} open)."
        )
    return None


def register_core_guards() -> None:
    """Idempotently (re)install the core guards."""
    guards.remove_owner("core")
    sr, lease = "service_request", "lease_return"
    chain = [
        "send_invitation",
        "send_campus_info",
        "send_reminder_1",
        "send_reminder_2",
        "escalate",
    ]
    guards.add(sr, "send_invitation", Guard("not_campus", not_campus, UJ))
    guards.add(sr, "send_campus_info", Guard("is_campus", is_campus, UJ))
    guards.add(
        sr,
        ["send_invitation", "send_reminder_1", "send_reminder_2", "escalate"],
        Guard("region_resolved", region_resolved, UJ),
    )
    guards.add(sr, chain, Guard("contact_email", contact_email, UJ))
    guards.add(
        sr, "book_appointment", Guard("start_in_future", start_in_future, needs_payload=True)
    )
    guards.add(sr, "claim", Guard("has_appointment", has_appointment))
    guards.add(sr, ["finish_personalization", "skip_preparation"], Guard("is_editor", is_editor))
    guards.add(sr, "release", Guard("is_editor_or_admin", is_editor_or_admin))
    guards.add(
        sr,
        ["finish_personalization", "skip_preparation", "close_order"],
        Guard("all_new_items_assigned", all_new_items_assigned),
    )
    guards.add(sr, "close_order", Guard("software_checklist_complete", software_checklist_complete))
    guards.add(
        sr, "mark_provided", Guard("delivery_type_is_technician", delivery_type_is_technician)
    )
    guards.add(
        sr,
        ["ship", "close_without_label"],
        Guard("delivery_type_is_parcel", delivery_type_is_parcel),
    )
    guards.add(sr, "ship", Guard("all_new_items_packed", all_new_items_packed))
    guards.add(
        lease,
        ["send_info", "send_reminder_1", "send_reminder_2", "escalate"],
        Guard("contact_email", contact_email, UJ),
    )


register_core_guards()

__all__ = ["LeaseReturn", "register_core_guards"]
