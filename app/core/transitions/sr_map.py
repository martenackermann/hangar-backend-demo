"""Service request transition map — SPEC §4.1.1 (authoritative)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.enums import (
    PRE_APPT,
    SR_NON_TERMINAL,
    WITH_APPT,
    ServiceRequestStatus,
    Trigger,
)
from app.core.statemachine import NEW, Transition, TransitionMap
from app.core.timeutil import business_today

if TYPE_CHECKING:
    from app.core.models import ServiceRequest, User

SR = ServiceRequestStatus
U, J, IM, S, Y = Trigger.USER, Trigger.JOB, Trigger.IMPORT, Trigger.SYNC, Trigger.SYSTEM


def _set(*statuses: SR | str) -> frozenset[str]:
    return frozenset(str(s) for s in statuses)


def _imported_or_vip(sr: ServiceRequest) -> str:
    return SR.IMPORTED_VIP if sr.is_vip else SR.IMPORTED


def _booked_target(sr: ServiceRequest) -> str:
    if sr.status == SR.RESCHEDULE_NEEDED and sr.resume_status is not None:
        return sr.resume_status
    return SR.APPOINTMENT_BOOKED


def _resolve_next_mail(sr: ServiceRequest) -> str | None:
    return {
        SR.IMPORTED: "send_campus_info" if sr.is_campus else "send_invitation",
        SR.INVITED: "send_reminder_1",
        SR.REMINDER_1_SENT: "send_reminder_2",
        SR.REMINDER_2_SENT: "escalate",
    }.get(sr.status)


def _is_paused(sr: Any, _actor: User | None) -> bool:
    resume = sr.communication_resume_date
    return bool(sr.communication_paused) or (resume is not None and resume > business_today())


def _claim_allowed(sr: ServiceRequest, actor: User | None) -> bool:
    if sr.status == SR.APPOINTMENT_BOOKED:
        return True
    # take-over: only when someone else holds it
    return actor is not None and sr.editor_user_id != actor.id


def _skip_allowed(sr: ServiceRequest, _actor: User | None) -> bool:
    if sr.status == SR.PERSONALIZATION_IN_PROGRESS:
        return True
    return not sr.new_items  # appointment_booked only without new items


_MAIL_FROM = _set(SR.IMPORTED, SR.INVITED, SR.REMINDER_1_SENT, SR.REMINDER_2_SENT)
_HOLD_FROM = _set(
    SR.IMPORTED,
    SR.INVITED,
    SR.CAMPUS_INFO_SENT,
    SR.REMINDER_1_SENT,
    SR.REMINDER_2_SENT,
    SR.ESCALATED,
)
_ON_SITE = ("tours.execute_own", "tours.execute_any")

SR_TRANSITIONS = TransitionMap(
    "service_request",
    [
        Transition(
            "import_create",
            frozenset({NEW}),
            _imported_or_vip,
            "imports.create",
            frozenset({IM}),
            "Import",
            "h2",
            internal=True,
        ),
        Transition(
            "send_invitation",
            _set(SR.IMPORTED),
            SR.INVITED,
            "sr.comms",
            frozenset({U, J}),
            "Send booking invitation",
            "h1",
            bulk=True,
        ),
        Transition(
            "send_campus_info",
            _set(SR.IMPORTED),
            SR.CAMPUS_INFO_SENT,
            "sr.comms",
            frozenset({U, J}),
            "Send campus info",
            "h1",
            bulk=True,
        ),
        Transition(
            "send_reminder_1",
            _set(SR.INVITED),
            SR.REMINDER_1_SENT,
            "sr.comms",
            frozenset({U, J}),
            "Send reminder 1",
            "h1",
            bulk=True,
        ),
        Transition(
            "send_reminder_2",
            _set(SR.REMINDER_1_SENT),
            SR.REMINDER_2_SENT,
            "sr.comms",
            frozenset({U, J}),
            "Send reminder 2",
            "h1",
            bulk=True,
        ),
        Transition(
            "escalate",
            _set(SR.REMINDER_2_SENT),
            SR.ESCALATED,
            "sr.comms",
            frozenset({U, J}),
            "Escalate",
            "h1",
            bulk=True,
        ),
        Transition(
            "send_next_mail",
            _MAIL_FROM,
            None,
            "sr.comms",
            frozenset({U}),
            "Send next mail",
            "h1",
            bulk=True,
            resolve=_resolve_next_mail,
        ),
        Transition(
            "mark_hardware_unavailable",
            _HOLD_FROM,
            SR.HARDWARE_UNAVAILABLE,
            "sr.comms",
            frozenset({U, IM}),
            "Mark hardware unavailable",
            "h1",
            bulk=True,
        ),
        Transition(
            "release_hardware_hold",
            _set(SR.HARDWARE_UNAVAILABLE),
            SR.IMPORTED,
            "sr.comms",
            frozenset({U}),
            "Release hardware hold",
            "h1",
            bulk=True,
        ),
        Transition(
            "release_vip",
            _set(SR.IMPORTED_VIP),
            SR.IMPORTED,
            "sr.comms",
            frozenset({U}),
            "Release to standard flow",
            "h1",
            bulk=True,
        ),
        Transition(
            "pause_communication",
            frozenset(SR_NON_TERMINAL),
            None,
            "sr.comms",
            frozenset({U}),
            "Pause communication",
            "h1",
            bulk=True,
        ),
        Transition(
            "resume_communication",
            frozenset(SR_NON_TERMINAL),
            None,
            "sr.comms",
            frozenset({U}),
            "Resume communication",
            "h1",
            bulk=True,
            condition=_is_paused,
        ),
        Transition(
            "book_appointment",
            frozenset(PRE_APPT),
            _booked_target,
            "appointments.manage",
            frozenset({U}),
            "Book appointment",
            "h4",
        ),
        Transition(
            "appointment_synced",
            frozenset(PRE_APPT),
            _booked_target,
            None,
            frozenset({S}),
            "Appointment synced",
            "h4",
            internal=True,
        ),
        Transition(
            "appointment_cancelled",
            frozenset(WITH_APPT),
            SR.RESCHEDULE_NEEDED,
            None,
            frozenset({S}),
            "Appointment cancelled",
            "h4",
            internal=True,
        ),
        Transition(
            "request_reschedule",
            frozenset(WITH_APPT),
            SR.RESCHEDULE_NEEDED,
            "appointments.manage",
            frozenset({U}),
            "Request reschedule",
            "h4",
        ),
        Transition(
            "claim",
            _set(SR.APPOINTMENT_BOOKED, SR.PERSONALIZATION_IN_PROGRESS),
            SR.PERSONALIZATION_IN_PROGRESS,
            "staging.personalize",
            frozenset({U}),
            "Claim",
            "h3",
            condition=_claim_allowed,
        ),
        Transition(
            "release",
            _set(SR.PERSONALIZATION_IN_PROGRESS),
            SR.APPOINTMENT_BOOKED,
            "staging.personalize",
            frozenset({U}),
            "Release",
            "h3",
        ),
        Transition(
            "finish_personalization",
            _set(SR.PERSONALIZATION_IN_PROGRESS),
            SR.PERSONALIZATION_DONE,
            "staging.personalize",
            frozenset({U}),
            "Finish personalization",
            "h3",
        ),
        Transition(
            "skip_preparation",
            _set(SR.PERSONALIZATION_IN_PROGRESS, SR.APPOINTMENT_BOOKED),
            SR.READY_FOR_PICKING,
            "staging.personalize",
            frozenset({U}),
            "Skip preparation",
            "h3",
            condition=_skip_allowed,
        ),
        Transition(
            "reopen_personalization",
            _set(SR.PERSONALIZATION_DONE),
            SR.PERSONALIZATION_IN_PROGRESS,
            "staging.personalize",
            frozenset({U}),
            "Reopen personalization",
            "h3",
        ),
        Transition(
            "close_order",
            _set(SR.PERSONALIZATION_DONE),
            SR.READY_FOR_PICKING,
            "staging.close",
            frozenset({U}),
            "Close order",
            "h3",
        ),
        Transition(
            "mark_provided",
            _set(SR.READY_FOR_PICKING),
            SR.PROVIDED,
            "fulfilment.pick",
            frozenset({U}),
            "Mark provided",
            "h3",
            bulk=True,
        ),
        Transition(
            "ship",
            _set(SR.READY_FOR_PICKING),
            SR.SHIPPED,
            "fulfilment.ship",
            frozenset({U}),
            "Create shipment",
            "h3",
            via_endpoint=False,
        ),
        Transition(
            "close_without_label",
            _set(SR.READY_FOR_PICKING),
            SR.PROVIDED,
            "fulfilment.ship",
            frozenset({U}),
            "Close without label",
            "h3",
            requires_comment=True,
        ),
        Transition(
            "complete_deployment",
            _set(SR.PROVIDED, SR.SHIPPED),
            SR.COMPLETED,
            _ON_SITE,
            frozenset({U}),
            "Complete",
            "h4",
        ),
        Transition(
            "reschedule_on_site",
            _set(SR.PROVIDED, SR.SHIPPED),
            SR.RESCHEDULE_NEEDED,
            _ON_SITE,
            frozenset({U}),
            "Reschedule",
            "h4",
        ),
        Transition(
            "cancel_on_site",
            _set(SR.PROVIDED, SR.SHIPPED),
            SR.CANCELLED,
            _ON_SITE,
            frozenset({U}),
            "Cancel on site",
            "h4",
            destructive=True,
        ),
        Transition(
            "complete_manually",
            _set(SR.PROVIDED, SR.SHIPPED),
            SR.COMPLETED,
            "sr.manage",
            frozenset({U}),
            "Complete manually",
            "h4",
            requires_comment=True,
        ),
        Transition(
            "cancel",
            frozenset(SR_NON_TERMINAL),
            SR.CANCELLED,
            "sr.manage",
            frozenset({U, IM}),
            "Cancel request",
            "h1",
            bulk=True,
            requires_comment=True,
            destructive=True,
        ),
        Transition(
            "reopen",
            _set(SR.CANCELLED),
            _imported_or_vip,
            "sr.manage",
            frozenset({U}),
            "Reopen",
            "h1",
        ),
    ],
)
