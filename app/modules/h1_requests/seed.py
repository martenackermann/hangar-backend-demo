"""H1 seeder (order 10, SPEC §11) and seed.json exporter.

- ``dispatch_settings``: the §5.2 defaults.
- ``mail_log``: one row per mail-implying ``status_transitions`` row of the core story
  (chain mails, hardware hold, reschedules, shipping confirmations, protocols) — rendered
  with the H1 templates in their historical state; other slices' templates get the
  catalogue subject only (their tables do not exist yet at order 10).
- ``dispatch_runs``: the daily 07:00 runs of T−13 … T (today's only after 07:00) whose
  counts equal the job-triggered chain transitions of that day, each with its
  ``background_jobs`` row, plus one dry run by Dana computed live on the seeded data.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.demo import table_rows
from app.core.enums import (
    JobKind,
    JobStatus,
    LeaseReturnStatus,
    MailStatus,
    ServiceRequestStatus,
    Trigger,
)
from app.core.models import BackgroundJob, LeaseReturn, ServiceRequest, StatusTransition, User
from app.core.notifications import render_template, templates
from app.core.seed.framework import SeedContext
from app.core.timeutil import business_datetime, business_hour, to_business_date

from .chain import DISPATCH_HOUR, LEASE_TEMPLATES, SR_TEMPLATES, default_settings
from .dispatch import execute, store_outcome
from .mails import escalation_cc, lease_context, sr_context
from .models import DispatchRun, DispatchSettings, MailLog

log = structlog.get_logger(__name__)

SR = ServiceRequestStatus
LR = LeaseReturnStatus
DANA = "dana.dispatcher@example.com"
RUN_DAYS = 14
#: other slices' templates: catalogue subjects (SPEC §10), body not stored
FOREIGN_SUBJECTS = {
    "sr.shipping_confirmation": "Your hardware for {number} is on its way",
    "sr.protocol": "Handover protocol for {number}",
}
RESCHEDULE_ACTIONS = {"appointment_cancelled", "request_reschedule", "reschedule_on_site"}
LEASE_LABELS = {
    "send_info": "Info mail",
    "send_reminder_1": "Reminder 1",
    "send_reminder_2": "Reminder 2",
}
#: mails left ``failed`` in the story (mailbox full / unknown) — realistic audit content
FAILED_MAILS = 2


async def seed_comms(session: AsyncSession, ctx: SeedContext) -> None:
    if await session.get(DispatchSettings, 1) is None:
        session.add(default_settings())
        await session.flush()
    if await session.scalar(select(func.count()).select_from(MailLog)):
        log.info("h1_seed_skipped", reason="mail_log exists")
        return
    mails = await seed_mail_log(session, ctx)
    runs = await seed_dispatch_runs(session, ctx)
    log.info("h1_seed_done", mails=mails, dispatch_runs=runs)


# --- mail log ----------------------------------------------------------------------------------------


def _template_for(row: StatusTransition, entity: Any) -> str | None:
    payload = row.payload or {}
    if row.entity_type == "lease_return":
        template = LEASE_TEMPLATES.get(row.action)
        if template and entity.is_campus:
            template = f"{template}_campus"
        return template
    if row.action in SR_TEMPLATES:
        return SR_TEMPLATES[row.action]
    if row.action == "mark_hardware_unavailable":
        return "sr.hardware_unavailable"
    if row.action in RESCHEDULE_ACTIONS:
        if row.action == "request_reschedule" and payload.get("notifyUser") is False:
            return None
        return "sr.reschedule_campus" if entity.is_campus else "sr.reschedule"
    if row.action == "ship" and payload.get("sendConfirmation", True):
        return "sr.shipping_confirmation"
    if row.action == "complete_deployment" and payload.get("sendProtocol", True):
        return "sr.protocol"
    return None


class _History:
    """Per-entity mail history while replaying transitions chronologically."""

    def __init__(self) -> None:
        self.last_chain_mail: dict[int, datetime] = {}
        self.lease_mails: dict[int, list[tuple[str, date]]] = defaultdict(list)


def _sr_mail_context(
    sr: ServiceRequest,
    row: StatusTransition,
    history: _History,
    deviation_labels: dict[int, str],
    reschedules: dict[Any, Any],
) -> dict[str, Any]:
    context = sr_context(sr)
    previous = history.last_chain_mail.get(sr.id)
    context["invited_on"] = to_business_date(previous).isoformat() if previous else None
    if row.action in RESCHEDULE_ACTIONS:
        info = reschedules.get(sr.id) or reschedules.get(str(sr.id)) or {}
        context["appointment_start"] = info.get("start")
        code = (row.payload or {}).get("deviationCodeId")
        context["deviation_label"] = deviation_labels.get(code) if isinstance(code, int) else None
    else:
        context["deviation_label"] = None
    return context


def _lease_mail_context(
    lease: LeaseReturn, row: StatusTransition, history: _History
) -> dict[str, Any]:
    context = lease_context(lease)
    context["days_left"] = (lease.retirement_date - to_business_date(row.occurred_at)).days
    context["prior_mails"] = [
        {"label": label, "date": at.isoformat()} for label, at in history.lease_mails[lease.id]
    ]
    return context


async def seed_mail_log(session: AsyncSession, ctx: SeedContext) -> int:
    from app.core.models import DeviationCode

    rng = ctx.rng
    srs = {s.id: s for s in (await session.scalars(select(ServiceRequest))).all()}
    leases = {lease.id: lease for lease in (await session.scalars(select(LeaseReturn))).all()}
    deviation_labels = {d.id: d.label for d in (await session.scalars(select(DeviationCode))).all()}
    reschedules: dict[Any, Any] = ctx.data.get("core", {}).get("reschedule_history", {})
    rows = (
        await session.scalars(
            select(StatusTransition)
            .where(StatusTransition.entity_type.in_(("service_request", "lease_return")))
            .order_by(StatusTransition.occurred_at, StatusTransition.id)
        )
    ).all()
    history = _History()
    mails: list[MailLog] = []
    for row in rows:
        entity: Any = (srs if row.entity_type == "service_request" else leases).get(row.entity_id)
        if entity is None:
            continue
        template_id = _template_for(row, entity)
        email = entity.contact.email if entity.contact else None
        if template_id is None or not email:
            continue
        cc: list[str] = []
        attachments: list[int] = []
        subject: str | None = None
        html: str | None = None
        text: str | None = None
        if row.entity_type == "service_request":
            label = entity.number
            if template_id == "sr.escalation":
                cc = escalation_cc()
            if template_id in FOREIGN_SUBJECTS:
                subject = FOREIGN_SUBJECTS[template_id].format(number=entity.number)
                if template_id == "sr.protocol" and entity.protocol_file_id:
                    attachments = [entity.protocol_file_id]
            else:
                context = _sr_mail_context(entity, row, history, deviation_labels, reschedules)
                rendered = render_template(templates.get(template_id), context)
                subject, html, text = rendered.subject, rendered.html, rendered.text
            if row.action in SR_TEMPLATES:
                history.last_chain_mail[entity.id] = row.occurred_at
        else:
            label = entity.asset_tag
            if row.action == "escalate":
                cc = escalation_cc(entity.escalation_email)
            context = _lease_mail_context(entity, row, history)
            rendered = render_template(templates.get(template_id), context)
            subject, html, text = rendered.subject, rendered.html, rendered.text
            if row.action in LEASE_LABELS:
                history.lease_mails[entity.id].append(
                    (LEASE_LABELS[row.action], to_business_date(row.occurred_at))
                )
        sent_at = row.occurred_at + timedelta(seconds=rng.randint(20, 150))
        mails.append(
            MailLog(
                notification_id=str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                template_id=template_id,
                status=MailStatus.SENT,
                to_addresses=[email],
                cc_addresses=cc,
                subject=subject,
                body_html=html,
                body_text=text,
                entity_type=row.entity_type,
                entity_id=entity.id,
                entity_label=label,
                attachment_file_ids=attachments,
                message_id=f"<seed-{len(mails) + 1:05d}.{rng.randint(1000, 9999)}@mail.example.com>",
                trigger=row.trigger,
                triggered_by=row.actor_user_id,
                queued_at=row.occurred_at,
                sent_at=sent_at,
            )
        )
    # a couple of bounces, deterministic: the oldest lease reminders
    reminders = [m for m in mails if m.template_id.startswith("lease.reminder_1")][:FAILED_MAILS]
    for mail in reminders:
        mail.status = MailStatus.FAILED
        mail.sent_at = None
        mail.message_id = None
        mail.error = "SMTP 552 5.2.2 Mailbox full (after 3 attempts)"
    session.add_all(mails)
    await session.flush()
    return len(mails)


# --- dispatch runs -----------------------------------------------------------------------------------


def _chain_template(row: StatusTransition, entity: Any) -> str | None:
    if row.entity_type == "service_request":
        return SR_TEMPLATES.get(row.action)
    template = LEASE_TEMPLATES.get(row.action)
    if template and entity is not None and entity.is_campus:
        template = f"{template}_campus"
    return template


async def seed_dispatch_runs(session: AsyncSession, ctx: SeedContext) -> int:
    rng = ctx.rng
    first_day = ctx.today - timedelta(days=RUN_DAYS - 1)
    srs = {s.id: s for s in (await session.scalars(select(ServiceRequest))).all()}
    leases = {lease.id: lease for lease in (await session.scalars(select(LeaseReturn))).all()}
    job_rows = (
        await session.scalars(
            select(StatusTransition).where(
                StatusTransition.trigger == Trigger.JOB,
                StatusTransition.occurred_at >= business_datetime(first_day, 0),
            )
        )
    ).all()
    counts: dict[date, Counter[str]] = defaultdict(Counter)
    for row in job_rows:
        entity = (srs if row.entity_type == "service_request" else leases).get(row.entity_id)
        template = _chain_template(row, entity)
        if template:
            counts[to_business_date(row.occurred_at)][template] += 1

    no_email_srs = [
        s.imported_at
        for s in srs.values()
        if s.status == SR.IMPORTED and (s.contact is None or not s.contact.email)
    ]
    unresolved = [
        s.imported_at for s in srs.values() if s.status == SR.IMPORTED and s.region_id is None
    ]
    no_email_leases = [
        lease.imported_at
        for lease in leases.values()
        if lease.status == LR.IMPORTED and (lease.contact is None or not lease.contact.email)
    ]

    created = 0
    for offset in range(RUN_DAYS):
        day = first_day + timedelta(days=offset)
        started = business_datetime(day, DISPATCH_HOUR) + timedelta(seconds=rng.randint(2, 9))
        if started > ctx.now or (day == ctx.today and not counts.get(day)):
            continue  # today's run only when the story already sent today's mails
        finished = started + timedelta(seconds=rng.randint(14, 48))
        before = business_datetime(day, DISPATCH_HOUR)
        skipped: Counter[str] = Counter()
        skipped["contact_email"] = sum(1 for at in no_email_srs if at < before - timedelta(days=1))
        skipped["contact_email"] += sum(1 for at in no_email_leases if at < before)
        skipped["region_resolved"] = sum(1 for at in unresolved if at < before - timedelta(days=1))
        run_counts = dict(counts.get(day, Counter()))
        job = BackgroundJob(
            kind=JobKind.COMMS_DISPATCH,
            status=JobStatus.SUCCEEDED,
            progress_processed=sum(run_counts.values()) + sum(skipped.values()),
            progress_total=sum(run_counts.values()) + sum(skipped.values()),
            message=f"{sum(run_counts.values())} mails sent",
            started_at=started,
            finished_at=finished,
            created_at=started,
            updated_at=finished,
        )
        session.add(job)
        await session.flush()
        run = DispatchRun(
            background_job_id=job.id,
            trigger=Trigger.JOB,
            dry_run=False,
            status=JobStatus.SUCCEEDED,
            started_at=started,
            finished_at=finished,
            counts=run_counts,
            skipped={k: v for k, v in skipped.items() if v},
            errors=[],
        )
        session.add(run)
        await session.flush()
        job.result = {
            "kind": "comms_dispatch",
            "dispatchRunId": run.id,
            "counts": run_counts,
            "skipped": run.skipped,
            "errorCount": 0,
        }
        created += 1

    # one dry run by Dana, computed live on the seeded data
    dana = (await session.scalars(select(User).where(User.email == DANA))).first()
    if dana is not None:
        await session.flush()
        started = ctx.now - timedelta(minutes=25 if business_hour(ctx.now) >= 8 else 5)
        run = DispatchRun(
            trigger=Trigger.USER,
            dry_run=True,
            status=JobStatus.RUNNING,
            started_at=started,
            triggered_by=dana.id,
            counts={},
            skipped={},
            errors=[],
        )
        session.add(run)
        await session.flush()
        outcome = await execute(session, dry_run=True)
        store_outcome(run, outcome, JobStatus.SUCCEEDED)
        run.finished_at = started + timedelta(seconds=2)
        await session.flush()
        created += 1
    return created


# --- seed.json ---------------------------------------------------------------------------------------

#: newest mails keep their rendered HTML in seed.json (size); all keep the text body
EXPORT_HTML_ROWS = 150


async def export_comms(session: AsyncSession, ctx: SeedContext) -> dict[str, list[dict[str, Any]]]:
    mails = await table_rows(session, "mail_log")
    newest = sorted(mails, key=lambda r: (r["queuedAt"], r["id"]), reverse=True)
    keep = {r["id"] for r in newest[:EXPORT_HTML_ROWS]}
    for row in mails:
        if row["id"] not in keep:
            row["bodyHtml"] = None
    return {
        "dispatch_settings": await table_rows(session, "dispatch_settings"),
        "dispatch_runs": await table_rows(session, "dispatch_runs"),
        "mail_log": mails,
    }
