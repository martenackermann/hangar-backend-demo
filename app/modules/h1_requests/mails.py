"""H1 notification templates (SPEC §10) and their contexts.

Every SR template renders from :func:`sr_context`, every lease template from
:func:`lease_context`, so ``sample_context`` (preview without an entity), the real sends
and ``build_context`` (preview with an entity, SPEC_RESOLUTIONS #13) share one shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.models import DeviationCode, LeaseReturn, ServiceRequest
from app.core.notifications import NotificationTemplate
from app.core.read_models import booking_link
from app.core.schemas import EntityRef
from app.core.timeutil import business_today, to_business_date

TEMPLATES = Path(__file__).parent / "templates"


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    id: str
    name: str
    subject: str
    description: str
    trigger: str
    recipients: str
    entity_type: str


SR_SPECS: tuple[TemplateSpec, ...] = (
    TemplateSpec(
        "sr.invitation",
        "Booking invitation",
        "Book your hardware appointment for {{ sr_number }}",
        "Order confirmation with the booking link and the next steps.",
        "SR send_invitation (daily dispatch or manual)",
        "Contact",
        "service_request",
    ),
    TemplateSpec(
        "sr.campus_info",
        "Campus info",
        "Your hardware order {{ sr_number }}: a technician will contact you",
        "Campus users: a technician phones them to agree a date (no booking link).",
        "SR send_campus_info (daily dispatch or manual)",
        "Contact",
        "service_request",
    ),
    TemplateSpec(
        "sr.reminder_1",
        "Booking reminder 1",
        "Reminder: book your appointment for {{ sr_number }}",
        "First reminder with the booking link.",
        "SR send_reminder_1",
        "Contact",
        "service_request",
    ),
    TemplateSpec(
        "sr.reminder_2",
        "Booking reminder 2",
        "Second reminder: appointment for {{ sr_number }} still open",
        "Second reminder with the booking link.",
        "SR send_reminder_2",
        "Contact",
        "service_request",
    ),
    TemplateSpec(
        "sr.escalation",
        "Booking escalation",
        "Urgent: no appointment booked for {{ sr_number }}",
        "Escalation: security and lease-cost reasons, booking link.",
        "SR escalate",
        "Contact; cc escalation mailbox",
        "service_request",
    ),
    TemplateSpec(
        "sr.hardware_unavailable",
        "Hardware unavailable",
        "Your order {{ sr_number }}: hardware currently unavailable",
        "Sent once when a request goes on hardware hold.",
        "SR enters hardware_unavailable (once per request)",
        "Contact",
        "service_request",
    ),
    TemplateSpec(
        "sr.reschedule",
        "Reschedule request",
        "Please book a new appointment for {{ sr_number }}",
        "The last appointment did not take place; booking link for a new date.",
        "SR enters reschedule_needed (not campus)",
        "Contact",
        "service_request",
    ),
    TemplateSpec(
        "sr.reschedule_campus",
        "Reschedule (campus)",
        "Your order {{ sr_number }}: we will contact you for a new date",
        "Campus variant: a technician phones the user for a new date.",
        "SR enters reschedule_needed (campus)",
        "Contact",
        "service_request",
    ),
)

_LEASE_BASE: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "lease.info",
        "Lease info",
        "Your leased device {{ asset_tag }} is due for return on {{ retirement_date | date }}",
        "Device and retirement date; please order a replacement via the ITSM portal.",
        "Lease send_info (daily dispatch or manual)",
        "Contact",
    ),
    (
        "lease.reminder_1",
        "Lease reminder 1",
        "Reminder: please order a replacement for {{ asset_tag }}",
        "Reminder with the days left until retirement.",
        "Lease send_reminder_1",
        "Contact",
    ),
    (
        "lease.reminder_2",
        "Lease reminder 2",
        "Second reminder: replacement for {{ asset_tag }} not yet ordered",
        "Stronger wording, cost impact of a late return.",
        "Lease send_reminder_2",
        "Contact",
    ),
    (
        "lease.escalation",
        "Lease escalation",
        "Action required: lease of {{ asset_tag }} ends {{ retirement_date | date }}",
        "Escalation notice with the list of earlier mails.",
        "Lease escalate",
        "Contact; cc escalation email (if set) and escalation mailbox",
    ),
)


def _lease_specs() -> tuple[TemplateSpec, ...]:
    specs: list[TemplateSpec] = []
    for template_id, name, subject, description, trigger, recipients in _LEASE_BASE:
        specs.append(
            TemplateSpec(
                template_id, name, subject, description, trigger, recipients, "lease_return"
            )
        )
        specs.append(
            TemplateSpec(
                f"{template_id}_campus",
                f"{name} (campus)",
                subject,
                f"{description} Campus variant: bring the device to the Campus service point.",
                f"{trigger}, campus users",
                recipients,
                "lease_return",
            )
        )
    return tuple(specs)


LEASE_SPECS = _lease_specs()


# --- contexts ------------------------------------------------------------------------------------


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def sr_context(sr: ServiceRequest, *, deviation_label: str | None = None) -> dict[str, Any]:
    contact = sr.contact
    appointment = sr.appointment_start_at
    return {
        "sr_number": sr.number,
        "gender": contact.gender if contact else None,
        "first_name": contact.first_name if contact else None,
        "last_name": contact.last_name if contact else "colleague",
        "contact_name": contact.display_name if contact else "",
        "items": [i.requested_type for i in sr.new_items if i.requested_type],
        "old_items": [i.expected_asset_tag for i in sr.old_items if i.expected_asset_tag],
        "booking_link": booking_link(sr),
        "region_name": sr.region.name if sr.region else None,
        "delivery_city": sr.delivery_city,
        "appointment_start": _iso(appointment),
        "deviation_label": deviation_label
        or (sr.deviation_code.label if sr.deviation_code else None),
        "invited_on": _iso(sr.communication_date),
    }


def _prior_mails(lease: LeaseReturn) -> list[dict[str, str]]:
    labels = (
        ("Info mail", lease.info_sent_at),
        ("Reminder 1", lease.reminder_1_sent_at),
        ("Reminder 2", lease.reminder_2_sent_at),
    )
    return [{"label": label, "date": at.isoformat()} for label, at in labels if at is not None]


def lease_context(lease: LeaseReturn) -> dict[str, Any]:
    contact = lease.contact
    return {
        "asset_tag": lease.asset_tag,
        "serial_number": lease.serial_number,
        "model": lease.model or lease.model_category or "leased device",
        "retirement_date": lease.retirement_date.isoformat(),
        "days_left": (lease.retirement_date - business_today()).days,
        "gender": contact.gender if contact else None,
        "first_name": contact.first_name if contact else None,
        "last_name": contact.last_name if contact else "colleague",
        "prior_mails": _prior_mails(lease),
    }


SR_SAMPLE: dict[str, Any] = {
    "sr_number": "SR0012345",
    "gender": "female",
    "first_name": "Mia",
    "last_name": "Turner",
    "contact_name": "Mia Turner",
    "items": ["Laptop Standard 14", "Docking Station USB-C", "Monitor 27"],
    "old_items": ["GX104211"],
    "booking_link": "https://booking.example.com/book/dest-westbay/svc-hw-rollout?ref=SR0012345",
    "region_name": "Westbay",
    "delivery_city": "Westbay",
    "appointment_start": "2026-10-14T07:00:00+00:00",
    "deviation_label": "User not present at the agreed time",
    "invited_on": "2026-10-06",
}


def _lease_sample() -> dict[str, Any]:
    today = business_today()
    retirement = today + timedelta(days=30)
    return {
        "asset_tag": "GX104211",
        "serial_number": "KD7Q2M9XAB",
        "model": "Aero 14 G2 Laptop",
        "retirement_date": retirement.isoformat(),
        "days_left": 30,
        "gender": "male",
        "first_name": "Noah",
        "last_name": "Brooks",
        "prior_mails": [
            {"label": "Info mail", "date": (today - timedelta(days=21)).isoformat()},
            {"label": "Reminder 1", "date": (today - timedelta(days=14)).isoformat()},
            {"label": "Reminder 2", "date": (today - timedelta(days=7)).isoformat()},
        ],
    }


async def _load_sr(session: AsyncSession, sr_id: int) -> ServiceRequest:
    sr = await session.get(ServiceRequest, sr_id)
    if sr is None:
        raise NotFoundError(f"Service request {sr_id} not found.")
    return sr


async def _load_lease(session: AsyncSession, lease_id: int) -> LeaseReturn:
    lease = await session.get(LeaseReturn, lease_id)
    if lease is None:
        raise NotFoundError(f"Lease return {lease_id} not found.")
    return lease


async def build_sr_context(session: AsyncSession, entity_id: int) -> dict[str, Any]:
    return sr_context(await _load_sr(session, entity_id))


async def build_lease_context(session: AsyncSession, entity_id: int) -> dict[str, Any]:
    return lease_context(await _load_lease(session, entity_id))


async def deviation_label(session: AsyncSession, code_id: Any) -> str | None:
    if not isinstance(code_id, int):
        return None
    return await session.scalar(select(DeviationCode.label).where(DeviationCode.id == code_id))


def sr_entity(sr: ServiceRequest) -> EntityRef:
    return EntityRef(type="service_request", id=sr.id, label=sr.number)


def lease_entity(lease: LeaseReturn) -> EntityRef:
    return EntityRef(type="lease_return", id=lease.id, label=lease.asset_tag)


def escalation_cc(extra: str | None = None) -> list[str]:
    settings = get_settings()
    cc = [address for address in (extra, settings.mail_escalation_cc) if address]
    return list(dict.fromkeys(cc))


def business_date_iso(value: datetime) -> str:
    return to_business_date(value).isoformat()


def notification_templates() -> list[NotificationTemplate]:
    result: list[NotificationTemplate] = []
    for spec in SR_SPECS:
        result.append(
            NotificationTemplate(
                id=spec.id,
                owner="h1",
                name=spec.name,
                subject=spec.subject,
                html=TEMPLATES / f"{spec.id}.html.j2",
                text=TEMPLATES / f"{spec.id}.txt.j2",
                description=spec.description,
                trigger=spec.trigger,
                recipients=spec.recipients,
                entity_type=spec.entity_type,
                sample_context=dict(SR_SAMPLE),
                build_context=build_sr_context,
            )
        )
    for spec in LEASE_SPECS:
        result.append(
            NotificationTemplate(
                id=spec.id,
                owner="h1",
                name=spec.name,
                subject=spec.subject,
                html=TEMPLATES / f"{spec.id}.html.j2",
                text=TEMPLATES / f"{spec.id}.txt.j2",
                description=spec.description,
                trigger=spec.trigger,
                recipients=spec.recipients,
                entity_type=spec.entity_type,
                sample_context=_lease_sample(),
                build_context=build_lease_context,
            )
        )
    return result
