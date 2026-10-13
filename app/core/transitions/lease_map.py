"""Lease return transition map — SPEC §4.2."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.enums import LR_NON_TERMINAL, LeaseReturnStatus, Trigger
from app.core.statemachine import NEW, Transition, TransitionMap
from app.core.timeutil import business_today

if TYPE_CHECKING:
    from app.core.models import LeaseReturn, User

LR = LeaseReturnStatus
U, J, IM, Y = Trigger.USER, Trigger.JOB, Trigger.IMPORT, Trigger.SYSTEM


def _set(*statuses: LR | str) -> frozenset[str]:
    return frozenset(str(s) for s in statuses)


def restored_status(lease: LeaseReturn, *, ignore_link: bool = False) -> LR:
    """SPEC §4.2 "Restored status" (used by ``unlink_service_request`` and ``reopen``)."""
    if lease.service_request_id is not None and not ignore_link:
        return LR.REPLACEMENT_ORDERED
    if lease.escalation_sent_at is not None:
        return LR.ESCALATED
    if lease.reminder_2_sent_at is not None:
        return LR.REMINDER_2_SENT
    if lease.reminder_1_sent_at is not None:
        return LR.REMINDER_1_SENT
    if lease.info_sent_at is not None:
        return LR.INFO_SENT
    return LR.IMPORTED_VIP if lease.is_vip else LR.IMPORTED


def _imported_or_vip(lease: LeaseReturn) -> str:
    return LR.IMPORTED_VIP if lease.is_vip else LR.IMPORTED


def _resolve_next_mail(lease: LeaseReturn) -> str | None:
    return {
        LR.IMPORTED: "send_info",
        LR.INFO_SENT: "send_reminder_1",
        LR.REMINDER_1_SENT: "send_reminder_2",
        LR.REMINDER_2_SENT: "escalate",
    }.get(lease.status)


def _is_paused(lease: LeaseReturn, _actor: User | None) -> bool:
    resume = lease.communication_resume_date
    return bool(lease.communication_paused) or (resume is not None and resume > business_today())


_OPEN_CHAIN = _set(
    LR.IMPORTED, LR.IMPORTED_VIP, LR.INFO_SENT, LR.REMINDER_1_SENT, LR.REMINDER_2_SENT, LR.ESCALATED
)

LEASE_TRANSITIONS = TransitionMap(
    "lease_return",
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
            "send_info",
            _set(LR.IMPORTED),
            LR.INFO_SENT,
            "lease.manage",
            frozenset({U, J}),
            "Send info mail",
            "h1",
            bulk=True,
        ),
        Transition(
            "send_reminder_1",
            _set(LR.INFO_SENT),
            LR.REMINDER_1_SENT,
            "lease.manage",
            frozenset({U, J}),
            "Send reminder 1",
            "h1",
            bulk=True,
        ),
        Transition(
            "send_reminder_2",
            _set(LR.REMINDER_1_SENT),
            LR.REMINDER_2_SENT,
            "lease.manage",
            frozenset({U, J}),
            "Send reminder 2",
            "h1",
            bulk=True,
        ),
        Transition(
            "escalate",
            _set(LR.REMINDER_2_SENT),
            LR.ESCALATED,
            "lease.manage",
            frozenset({U, J}),
            "Escalate",
            "h1",
            bulk=True,
        ),
        Transition(
            "send_next_mail",
            _set(LR.IMPORTED, LR.INFO_SENT, LR.REMINDER_1_SENT, LR.REMINDER_2_SENT),
            None,
            "lease.manage",
            frozenset({U}),
            "Send next mail",
            "h1",
            bulk=True,
            resolve=_resolve_next_mail,
        ),
        Transition(
            "release_vip",
            _set(LR.IMPORTED_VIP),
            LR.IMPORTED,
            "lease.manage",
            frozenset({U}),
            "Release to standard flow",
            "h1",
            bulk=True,
        ),
        Transition(
            "link_service_request",
            _OPEN_CHAIN | _set(LR.PROBABLY_CLOSED),
            LR.REPLACEMENT_ORDERED,
            "lease.manage",
            frozenset({IM, U}),
            "Link service request",
            "h1",
        ),
        Transition(
            "unlink_service_request",
            _set(LR.REPLACEMENT_ORDERED),
            lambda lease: restored_status(lease, ignore_link=True),
            "lease.manage",
            frozenset({U, Y}),
            "Unlink service request",
            "h1",
        ),
        Transition(
            "mark_probably_closed",
            _OPEN_CHAIN,
            LR.PROBABLY_CLOSED,
            "imports.create",
            frozenset({IM}),
            "Mark probably closed",
            "h2",
            internal=True,
        ),
        Transition(
            "close",
            frozenset(set(map(str, LR)) - {LR.CLOSED.value}),
            LR.CLOSED,
            "lease.manage",
            frozenset({U, Y}),
            "Close",
            "h1",
            bulk=True,
            requires_comment=True,
            destructive=True,
        ),
        Transition(
            "reopen",
            _set(LR.CLOSED, LR.PROBABLY_CLOSED),
            restored_status,
            "lease.manage",
            frozenset({U, IM}),
            "Reopen",
            "h1",
        ),
        Transition(
            "pause_communication",
            frozenset(map(str, LR_NON_TERMINAL)),
            None,
            "lease.manage",
            frozenset({U}),
            "Pause communication",
            "h1",
            bulk=True,
        ),
        Transition(
            "resume_communication",
            frozenset(map(str, LR_NON_TERMINAL)),
            None,
            "lease.manage",
            frozenset({U}),
            "Resume communication",
            "h1",
            bulk=True,
            condition=_is_paused,
        ),
    ],
)
