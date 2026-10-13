"""Attention flags — SPEC §6.14 (+ SPEC_RESOLUTIONS #37): Python computation for
serializers and SQL predicates for list filters (``attention=…`` / ``needsAttention``)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import ColumnElement, and_, exists, false, func, or_, select

from app.core.config import get_settings
from app.core.enums import (
    LR_NON_TERMINAL,
    SR_NON_TERMINAL,
    WITH_APPT,
    AttentionFlag,
    HoldSource,
    LeaseReturnStatus,
    ServiceRequestStatus,
)
from app.core.models import Contact, LeaseReturn, ServiceRequest, ServiceRequestItem
from app.core.timeutil import business_today, next_working_day, now_utc, to_business_date

F = AttentionFlag
SR = ServiceRequestStatus
LR = LeaseReturnStatus

SR_FLAGS: tuple[AttentionFlag, ...] = (
    F.REGION_UNRESOLVED,
    F.CONTACT_EMAIL_MISSING,
    F.UNKNOWN_ARTICLE,
    F.APPOINTMENT_SOON_NOT_READY,
    F.APPOINTMENT_OVERDUE,
    F.ESCALATED_STALE,
    F.ITSM_CLOSED,
    F.HOLD_GROUP_LEFT,
)
LEASE_FLAGS: tuple[AttentionFlag, ...] = (
    F.CONTACT_EMAIL_MISSING,
    F.ESCALATED_STALE,
    F.RETIREMENT_PASSED,
    F.NOT_COLLECTED,
)
_SOON = (SR.APPOINTMENT_BOOKED, SR.PERSONALIZATION_IN_PROGRESS, SR.PERSONALIZATION_DONE)
STALE_DAYS = 7


def _same_group(group: str | None) -> bool:
    hold = get_settings().itsm_hold_assignment_group.strip().casefold()
    return (group or "").strip().casefold() == hold


def sr_attention(
    sr: ServiceRequest, *, today: date | None = None, now: datetime | None = None
) -> list[AttentionFlag]:
    now = now or now_utc()
    today = today or business_today(now)
    flags: list[AttentionFlag] = []
    open_ = sr.status in SR_NON_TERMINAL
    if open_ and sr.region_id is None:
        flags.append(F.REGION_UNRESOLVED)
    if open_ and (sr.contact is None or not sr.contact.email):
        flags.append(F.CONTACT_EMAIL_MISSING)
    if open_ and any(item.article_id is None for item in sr.new_items):
        flags.append(F.UNKNOWN_ARTICLE)
    if (
        sr.status in _SOON
        and sr.appointment_start_at is not None
        and to_business_date(sr.appointment_start_at) <= next_working_day(today)
    ):
        flags.append(F.APPOINTMENT_SOON_NOT_READY)
    if (
        sr.status in WITH_APPT
        and sr.appointment_end_at is not None
        and sr.appointment_end_at < now - timedelta(hours=24)
    ):
        flags.append(F.APPOINTMENT_OVERDUE)
    if (
        sr.status == SR.ESCALATED
        and sr.communication_date is not None
        and sr.communication_date <= today - timedelta(days=STALE_DAYS)
    ):
        flags.append(F.ESCALATED_STALE)
    if open_ and sr.itsm_state == "Closed Complete":
        flags.append(F.ITSM_CLOSED)
    if (
        sr.status == SR.HARDWARE_UNAVAILABLE
        and sr.hold_source == HoldSource.IMPORT
        and not _same_group(sr.assignment_group)
    ):
        flags.append(F.HOLD_GROUP_LEFT)
    return flags


def lease_attention(lease: LeaseReturn, *, today: date | None = None) -> list[AttentionFlag]:
    today = today or business_today()
    flags: list[AttentionFlag] = []
    if lease.status in LR_NON_TERMINAL and (lease.contact is None or not lease.contact.email):
        flags.append(F.CONTACT_EMAIL_MISSING)
    if (
        lease.status == LR.ESCALATED
        and lease.communication_date is not None
        and lease.communication_date <= today - timedelta(days=STALE_DAYS)
    ):
        flags.append(F.ESCALATED_STALE)
    if lease.retirement_date < today and lease.status not in (LR.REPLACEMENT_ORDERED, LR.CLOSED):
        flags.append(F.RETIREMENT_PASSED)
    if (
        lease.status == LR.REPLACEMENT_ORDERED
        and lease.service_request is not None
        and lease.service_request.status == SR.COMPLETED
    ):
        flags.append(F.NOT_COLLECTED)
    return flags


# --- SQL predicates ---------------------------------------------------------------------------


def sr_attention_clause(
    flag: AttentionFlag, *, today: date | None = None, now: datetime | None = None
) -> ColumnElement[bool]:
    now = now or now_utc()
    today = today or business_today(now)
    open_ = ServiceRequest.status.in_(SR_NON_TERMINAL)
    tz = get_settings().business_tz
    match flag:
        case F.REGION_UNRESOLVED:
            return and_(open_, ServiceRequest.region_id.is_(None))
        case F.CONTACT_EMAIL_MISSING:
            no_email = exists().where(
                Contact.id == ServiceRequest.contact_id, Contact.email.is_(None)
            )
            return and_(open_, no_email)
        case F.UNKNOWN_ARTICLE:
            unknown = exists().where(
                ServiceRequestItem.service_request_id == ServiceRequest.id,
                func.left(ServiceRequestItem.slot, 4) == "new_",
                ServiceRequestItem.article_id.is_(None),
            )
            return and_(open_, unknown)
        case F.APPOINTMENT_SOON_NOT_READY:
            local_date = func.date(func.timezone(tz, ServiceRequest.appointment_start_at))
            return and_(ServiceRequest.status.in_(_SOON), local_date <= next_working_day(today))
        case F.APPOINTMENT_OVERDUE:
            return and_(
                ServiceRequest.status.in_(WITH_APPT),
                ServiceRequest.appointment_end_at < now - timedelta(hours=24),
            )
        case F.ESCALATED_STALE:
            return and_(
                ServiceRequest.status == SR.ESCALATED,
                ServiceRequest.communication_date <= today - timedelta(days=STALE_DAYS),
            )
        case F.ITSM_CLOSED:
            return and_(open_, ServiceRequest.itsm_state == "Closed Complete")
        case F.HOLD_GROUP_LEFT:
            hold = get_settings().itsm_hold_assignment_group.strip().lower()
            return and_(
                ServiceRequest.status == SR.HARDWARE_UNAVAILABLE,
                ServiceRequest.hold_source == HoldSource.IMPORT,
                func.lower(func.trim(func.coalesce(ServiceRequest.assignment_group, ""))) != hold,
            )
        case _:
            return false()


def sr_needs_attention_clause(
    *, today: date | None = None, now: datetime | None = None
) -> ColumnElement[bool]:
    return or_(*(sr_attention_clause(f, today=today, now=now) for f in SR_FLAGS))


def lease_attention_clause(
    flag: AttentionFlag, *, today: date | None = None
) -> ColumnElement[bool]:
    today = today or business_today()
    match flag:
        case F.CONTACT_EMAIL_MISSING:
            no_email = or_(
                LeaseReturn.contact_id.is_(None),
                exists().where(Contact.id == LeaseReturn.contact_id, Contact.email.is_(None)),
            )
            return and_(LeaseReturn.status.in_(LR_NON_TERMINAL), no_email)
        case F.ESCALATED_STALE:
            return and_(
                LeaseReturn.status == LR.ESCALATED,
                LeaseReturn.communication_date <= today - timedelta(days=STALE_DAYS),
            )
        case F.RETIREMENT_PASSED:
            return and_(
                LeaseReturn.retirement_date < today,
                LeaseReturn.status.not_in((LR.REPLACEMENT_ORDERED, LR.CLOSED)),
            )
        case F.NOT_COLLECTED:
            completed = exists(
                select(ServiceRequest.id).where(
                    ServiceRequest.id == LeaseReturn.service_request_id,
                    ServiceRequest.status == SR.COMPLETED,
                )
            )
            return and_(LeaseReturn.status == LR.REPLACEMENT_ORDERED, completed)
        case _:
            return false()


def lease_needs_attention_clause(*, today: date | None = None) -> ColumnElement[bool]:
    return or_(*(lease_attention_clause(f, today=today) for f in LEASE_FLAGS))
