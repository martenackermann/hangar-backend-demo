"""H1 event subscribers.

In-transaction ``Transitioned`` handlers implement the effects of the H1-owned actions
(SPEC §4.1.1 / §4.2), whatever the trigger (user, job, import, system), plus the H1 mails:

- chain mails (SR #2–#6, lease #2–#5) → ``sr.*`` / ``lease.*`` templates;
- SR enters ``hardware_unavailable`` → ``sr.hardware_unavailable`` once (setting);
- SR enters ``reschedule_needed`` → ``sr.reschedule[_campus]`` (#12: master switch **and**
  ``reschedule_mail_enabled``; ``request_reschedule`` only with ``notifyUser``);
- SR enters ``cancelled`` (any action, #31) → linked lease returns are unlinked;
- ``cancel``: new assets released, only new items lose ``asset_id`` (#50), editor cleared;
- ``reopen`` / ``release_hardware_hold``: chain restarts from today;
- lease ``close`` / ``reopen``: ``closed_at`` + ``close_reason``.

``NotificationQueued`` / ``NotificationSent`` / ``NotificationFailed`` maintain ``mail_log``
(SPEC §5.2, SPEC_RESOLUTIONS #14).
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assets import asset_service
from app.core.enums import (
    AssetAction,
    AssetStatus,
    HoldSource,
    LeaseReturnStatus,
    MailStatus,
    ServiceRequestStatus,
    Trigger,
)
from app.core.events import NotificationFailed, NotificationQueued, NotificationSent, Transitioned
from app.core.loading import ensure_loaded
from app.core.models import LeaseReturn, ServiceRequest, User
from app.core.notifications import notify
from app.core.services import unlink_lease_return
from app.core.timeutil import business_today, now_utc

from .chain import LEASE_CHAIN_ACTIONS, SR_CHAIN_ACTIONS, load_settings, template_for
from .mails import (
    deviation_label,
    escalation_cc,
    lease_context,
    lease_entity,
    sr_context,
    sr_entity,
)
from .models import MailLog

log = structlog.get_logger(__name__)

SR = ServiceRequestStatus
LR = LeaseReturnStatus
_RELEASABLE = (AssetStatus.ASSIGNED, AssetStatus.PREPARED, AssetStatus.PROVIDED)


async def on_transition(session: AsyncSession, event: Transitioned) -> None:
    entity = event.entity
    if entity is None:  # emitted without the ORM entity (tests / foreign publishers)
        return
    if event.entity_type == "service_request":
        await _sr_effects(session, entity, event)
    elif event.entity_type == "lease_return":
        await _lease_effects(session, entity, event)


# --- service requests ------------------------------------------------------------------------------


async def _sr_effects(session: AsyncSession, sr: ServiceRequest, event: Transitioned) -> None:
    await ensure_loaded(session, sr)
    action = event.action
    if action in SR_CHAIN_ACTIONS:
        await _send_sr_mail(
            session, sr, event, template_for("service_request", action, is_campus=False)
        )
    elif action == "mark_hardware_unavailable":
        await _hardware_hold(session, sr, event)
    elif action == "release_hardware_hold":
        sr.communication_date = None
        sr.communication_resume_date = business_today()
        sr.hold_source = None
    elif action == "reopen":
        sr.cancelled_at = None
        sr.communication_date = None
        sr.communication_resume_date = business_today()
    elif action == "cancel":
        await _cancel_effects(session, sr, event)
    if event.to_status == SR.RESCHEDULE_NEEDED and event.from_status != SR.RESCHEDULE_NEEDED:
        await _reschedule_mail(session, sr, event)
    if event.to_status == SR.CANCELLED and event.from_status != SR.CANCELLED:
        await _unlink_leases(session, sr)


async def _send_sr_mail(
    session: AsyncSession,
    sr: ServiceRequest,
    event: Transitioned,
    template_id: str | None,
    *,
    context: dict[str, Any] | None = None,
    cc: list[str] | None = None,
) -> str | None:
    email = sr.contact.email if sr.contact else None
    if template_id is None or not email:
        return None
    if template_id == "sr.escalation":
        cc = escalation_cc()
    return await notify(
        session,
        template_id,
        to=[email],
        cc=cc or [],
        context=context or sr_context(sr),
        entity=sr_entity(sr),
        trigger=event.trigger,
        actor=event.actor_id,
    )


async def _hardware_hold(session: AsyncSession, sr: ServiceRequest, event: Transitioned) -> None:
    if event.trigger == Trigger.USER:
        sr.hold_source = HoldSource.MANUAL
    elif sr.hold_source is None:
        sr.hold_source = HoldSource.IMPORT
    settings = await load_settings(session)
    if not settings.hardware_unavailable_mail_enabled or sr.hardware_unavailable_notified_at:
        return
    if await _send_sr_mail(session, sr, event, "sr.hardware_unavailable"):
        sr.hardware_unavailable_notified_at = now_utc()


async def _cancel_effects(session: AsyncSession, sr: ServiceRequest, event: Transitioned) -> None:
    sr.cancelled_at = now_utc()
    sr.editor_user_id = None
    sr.editor = None
    for item in sr.new_items:
        await ensure_loaded(session, item, "asset")
        asset = item.asset
        if asset is not None and asset.status in _RELEASABLE and asset.service_request_id == sr.id:
            await asset_service.move(
                session,
                asset,
                AssetAction.RELEASE,
                actor=None,
                trigger=Trigger.SYSTEM,
                reason=f"Service request {sr.number} cancelled",
            )
        item.asset_id = None
        item.asset = None
    await session.flush()


async def _reschedule_mail(session: AsyncSession, sr: ServiceRequest, event: Transitioned) -> None:
    settings = await load_settings(session)
    if not (settings.sr_mails_enabled and settings.reschedule_mail_enabled):
        return
    payload = event.payload or {}
    if event.action == "request_reschedule" and payload.get("notifyUser") is False:
        return
    label = await deviation_label(session, payload.get("deviationCodeId"))
    template_id = "sr.reschedule_campus" if sr.is_campus else "sr.reschedule"
    await _send_sr_mail(
        session, sr, event, template_id, context=sr_context(sr, deviation_label=label)
    )


async def _unlink_leases(session: AsyncSession, sr: ServiceRequest) -> None:
    leases = (
        await session.scalars(
            select(LeaseReturn).where(
                LeaseReturn.service_request_id == sr.id,
                LeaseReturn.status == LR.REPLACEMENT_ORDERED,
            )
        )
    ).all()
    for lease in leases:
        await unlink_lease_return(
            session,
            lease,
            actor=None,
            trigger=Trigger.SYSTEM,
            comment=f"Service request {sr.number} was cancelled.",
        )


# --- lease returns ---------------------------------------------------------------------------------


async def _lease_effects(session: AsyncSession, lease: LeaseReturn, event: Transitioned) -> None:
    action = event.action
    if action in LEASE_CHAIN_ACTIONS:
        await ensure_loaded(session, lease)
        email = lease.contact.email if lease.contact else None
        template_id = template_for("lease_return", action, is_campus=lease.is_campus)
        if email and template_id:
            cc = escalation_cc(lease.escalation_email) if action == "escalate" else []
            await notify(
                session,
                template_id,
                to=[email],
                cc=cc,
                context=lease_context(lease),
                entity=lease_entity(lease),
                trigger=event.trigger,
                actor=event.actor_id,
            )
    elif action == "close":
        payload = event.payload or {}
        # Reason precedence: payload `closeReason` (H5 deactivation: `device_deactivated`) →
        # a reason the caller already set in this transaction (H4 collection:
        # `device_collected`, with a human comment) → the comment → "Closed manually".
        # `reopen` clears both fields, so a value set here always comes from the caller.
        reason = (
            payload.get("closeReason")
            or payload.get("close_reason")
            or lease.close_reason
            or event.comment
            or "Closed manually"
        )
        lease.closed_at = lease.closed_at or now_utc()
        lease.close_reason = str(reason)[:200]
    elif action == "reopen":
        lease.closed_at = None
        lease.close_reason = None


# --- mail log ----------------------------------------------------------------------------------------


async def on_notification_queued(session: AsyncSession, event: NotificationQueued) -> None:
    entity = event.entity
    actor_id = event.actor_id
    if actor_id is not None and await session.get(User, actor_id) is None:
        actor_id = None  # unknown actor (e.g. a deleted user) — keep the mail, drop the link
    session.add(
        MailLog(
            notification_id=event.notification_id,
            template_id=event.template_id,
            status=MailStatus.QUEUED,
            to_addresses=list(event.to),
            cc_addresses=list(event.cc),
            entity_type=entity.type if entity else None,
            entity_id=entity.id if entity else None,
            entity_label=entity.label[:40] if entity else None,
            attachment_file_ids=list(event.attachment_file_ids),
            trigger=event.trigger,
            triggered_by=actor_id,
            queued_at=now_utc(),
        )
    )
    await session.flush()


async def on_notification_sent(session: AsyncSession, event: NotificationSent) -> None:
    await session.execute(
        update(MailLog)
        .where(MailLog.notification_id == event.notification_id)
        .values(
            status=MailStatus.SENT,
            subject=event.subject[:300],
            body_html=event.html,
            body_text=event.text,
            message_id=event.message_id[:200],
            sent_at=event.sent_at,
            error=None,
        )
    )


async def on_notification_failed(session: AsyncSession, event: NotificationFailed) -> None:
    await session.execute(
        update(MailLog)
        .where(MailLog.notification_id == event.notification_id)
        .values(status=MailStatus.FAILED, error=event.error)
    )
