"""Communication chain rules (SPEC §6.3, SPEC_RESOLUTIONS #9/#10).

Pure rules shared by the H1 guards (``chain_due``, ``not_paused``, ``retirement_window``),
the dispatch job's SQL candidate selection and the UI "next due" hint, so the three never
disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.enums import LeaseReturnStatus, ServiceRequestStatus
from app.core.models import LeaseReturn, ServiceRequest
from app.core.timeutil import business_datetime, business_today, now_utc, to_business_date

from .models import DispatchSettings

SR = ServiceRequestStatus
LR = LeaseReturnStatus
type EntityType = Literal["service_request", "lease_return"]

#: Hour (business time) of the scheduled daily dispatch (SPEC §9).
DISPATCH_HOUR = 7


@dataclass(frozen=True, slots=True)
class ChainStep:
    entity_type: EntityType
    action: str
    from_status: str
    #: ``dispatch_settings`` attribute holding the waiting days (``None`` = lease info window)
    days_attr: str
    #: template id (campus variant is derived by :func:`template_for`)
    template: str


SR_STEPS: tuple[ChainStep, ...] = (
    ChainStep("service_request", "send_invitation", SR.IMPORTED, "sr_invite_after_import_days", "sr.invitation"),
    ChainStep("service_request", "send_reminder_1", SR.INVITED, "sr_reminder_1_after_days", "sr.reminder_1"),
    ChainStep("service_request", "send_reminder_2", SR.REMINDER_1_SENT, "sr_reminder_2_after_days", "sr.reminder_2"),
    ChainStep("service_request", "escalate", SR.REMINDER_2_SENT, "sr_escalation_after_days", "sr.escalation"),
)  # fmt: skip
LEASE_STEPS: tuple[ChainStep, ...] = (
    ChainStep("lease_return", "send_info", LR.IMPORTED, "lease_info_days_before_retirement", "lease.info"),
    ChainStep("lease_return", "send_reminder_1", LR.INFO_SENT, "lease_reminder_1_after_days", "lease.reminder_1"),
    ChainStep("lease_return", "send_reminder_2", LR.REMINDER_1_SENT, "lease_reminder_2_after_days", "lease.reminder_2"),
    ChainStep("lease_return", "escalate", LR.REMINDER_2_SENT, "lease_escalation_after_days", "lease.escalation"),
)  # fmt: skip

#: SPEC §6.3 step 2: reverse order (no entity advances twice per run), leases before SRs.
DISPATCH_ORDER: tuple[ChainStep, ...] = (*reversed(LEASE_STEPS), *reversed(SR_STEPS))

SR_CHAIN_ACTIONS = ("send_invitation", "send_campus_info", "send_reminder_1", "send_reminder_2", "escalate")  # fmt: skip
LEASE_CHAIN_ACTIONS = ("send_info", "send_reminder_1", "send_reminder_2", "escalate")

SR_TEMPLATES: dict[str, str] = {
    "send_invitation": "sr.invitation",
    "send_campus_info": "sr.campus_info",
    "send_reminder_1": "sr.reminder_1",
    "send_reminder_2": "sr.reminder_2",
    "escalate": "sr.escalation",
}
LEASE_TEMPLATES: dict[str, str] = {
    "send_info": "lease.info",
    "send_reminder_1": "lease.reminder_1",
    "send_reminder_2": "lease.reminder_2",
    "escalate": "lease.escalation",
}


def template_for(entity_type: str, action: str, *, is_campus: bool) -> str | None:
    """Template id a chain action sends (campus variants for leases, SPEC §10)."""
    if entity_type == "service_request":
        return SR_TEMPLATES.get(action)
    template = LEASE_TEMPLATES.get(action)
    if template is None:
        return None
    return f"{template}_campus" if is_campus else template


def step_for(entity_type: str, action: str) -> ChainStep | None:
    steps = SR_STEPS if entity_type == "service_request" else LEASE_STEPS
    if action == "send_campus_info":
        action = "send_invitation"
    return next((s for s in steps if s.action == action), None)


def sr_first_action(sr: ServiceRequest) -> str:
    return "send_campus_info" if sr.is_campus else "send_invitation"


# --- settings ----------------------------------------------------------------------------------


def default_settings() -> DispatchSettings:
    """Transient row with the SPEC §5.2 defaults (used when the singleton is missing)."""
    from .models import SETTING_RANGES

    row = DispatchSettings(
        id=1,
        lease_mails_enabled=True,
        sr_mails_enabled=True,
        hardware_unavailable_mail_enabled=True,
        reschedule_mail_enabled=True,
        **{name: spec[0] for name, spec in SETTING_RANGES.items()},
    )
    return row


async def load_settings(session: AsyncSession) -> DispatchSettings:
    row = await session.get(DispatchSettings, 1)
    return row if row is not None else default_settings()


async def ensure_settings(session: AsyncSession) -> DispatchSettings:
    """The persistent singleton (created with defaults on first use)."""
    row = await session.get(DispatchSettings, 1)
    if row is None:
        row = default_settings()
        session.add(row)
        await session.flush()
    return row


def master_switch(settings: DispatchSettings, entity_type: str) -> bool:
    if entity_type == "service_request":
        return settings.sr_mails_enabled
    return settings.lease_mails_enabled


def next_scheduled_run(now: datetime | None = None) -> datetime:
    """Next 07:00 business time strictly after ``now``."""
    now = now or now_utc()
    today = to_business_date(now)
    candidate = business_datetime(today, DISPATCH_HOUR)
    if candidate <= now:
        candidate = business_datetime(today + timedelta(days=1), DISPATCH_HOUR)
    return candidate


# --- python rules (guards, UI hints) --------------------------------------------------------------


def is_paused(entity: Any, today: date | None = None) -> bool:
    """Paused indefinitely or with a resume date in the future (SPEC §6.3 "Eligibility")."""
    today = today or business_today()
    resume: date | None = entity.communication_resume_date
    return bool(entity.communication_paused) or (resume is not None and resume > today)


def base_date(entity: Any, step: ChainStep) -> date | None:
    if step.entity_type == "service_request" and step.action == "send_invitation":
        return to_business_date(entity.imported_at)
    if step.entity_type == "lease_return" and step.action == "send_info":
        return None
    value: date | None = entity.communication_date
    if value is None and entity.imported_at is not None:
        return to_business_date(entity.imported_at)
    return value


def due_date(entity: Any, step: ChainStep, settings: DispatchSettings) -> date | None:
    """First business date on which ``step`` is due for ``entity`` (``None`` = never)."""
    days: int = getattr(settings, step.days_attr)
    resume: date | None = entity.communication_resume_date
    if step.entity_type == "lease_return" and step.action == "send_info":
        window_opens: date = entity.retirement_date - timedelta(days=days)
        return max(window_opens, resume) if resume is not None else window_opens
    base = base_date(entity, step)
    if base is None:
        return None
    anchor = max(base, resume) if resume is not None else base
    return anchor + timedelta(days=days)


def is_due(entity: Any, step: ChainStep, settings: DispatchSettings, today: date) -> bool:
    due = due_date(entity, step, settings)
    return due is not None and due <= today


def retirement_window_open(lease: LeaseReturn, settings: DispatchSettings, today: date) -> bool:
    return lease.retirement_date <= today + timedelta(
        days=settings.lease_info_days_before_retirement
    )


def next_step(entity_type: str, entity: Any) -> ChainStep | None:
    steps = SR_STEPS if entity_type == "service_request" else LEASE_STEPS
    return next((s for s in steps if s.from_status == entity.status), None)


# --- SQL (dispatch candidate selection) ------------------------------------------------------------


def _business_date(column: Any) -> ColumnElement[Any]:
    return func.date(func.timezone(get_settings().business_tz, column))


def not_paused_clause(model: Any, today: date) -> ColumnElement[bool]:
    return and_(
        model.communication_paused.is_(False),
        or_(model.communication_resume_date.is_(None), model.communication_resume_date <= today),
    )


def due_clause(step: ChainStep, settings: DispatchSettings, today: date) -> ColumnElement[bool]:
    """SQL equivalent of :func:`is_due` (anchor = greatest(base, coalesce(resume, base)))."""
    days: int = getattr(settings, step.days_attr)
    if step.entity_type == "lease_return":
        model: Any = LeaseReturn
        if step.action == "send_info":
            return and_(
                LeaseReturn.retirement_date <= today + timedelta(days=days),
                func.coalesce(LeaseReturn.communication_resume_date, today) <= today,
            )
    else:
        model = ServiceRequest
    limit = today - timedelta(days=days)
    if step.entity_type == "service_request" and step.action == "send_invitation":
        base = _business_date(ServiceRequest.imported_at)
    else:
        base = func.coalesce(model.communication_date, _business_date(model.imported_at))
    return and_(
        base <= limit,
        or_(model.communication_resume_date.is_(None), model.communication_resume_date <= limit),
    )


def candidates_stmt(
    step: ChainStep, settings: DispatchSettings, today: date, *, after_id: int, limit: int
) -> Any:
    model: Any = ServiceRequest if step.entity_type == "service_request" else LeaseReturn
    return (
        select(model)
        .where(
            model.status == step.from_status,
            model.id > after_id,
            not_paused_clause(model, today),
            due_clause(step, settings, today),
        )
        .order_by(model.id)
        .limit(limit)
    )
