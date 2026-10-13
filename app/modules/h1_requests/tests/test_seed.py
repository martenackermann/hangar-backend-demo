"""H1 seed story (SPEC §11): settings, mail log consistent with transitions, dispatch runs."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import JobStatus, MailStatus, Trigger
from app.core.models import BackgroundJob, StatusTransition
from app.core.seed.framework import SeedContext
from app.core.timeutil import to_business_date
from app.modules.h1_requests.models import DispatchRun, DispatchSettings, MailLog
from app.modules.h1_requests.seed import EXPORT_HTML_ROWS, export_comms, seed_comms

CHAIN = ("send_invitation", "send_campus_info", "send_reminder_1", "send_reminder_2", "escalate", "send_info")  # fmt: skip


async def test_settings_and_mail_log(db_session: AsyncSession) -> None:
    settings = await db_session.get(DispatchSettings, 1)
    assert settings is not None
    assert (settings.max_mails_per_run, settings.sr_invite_after_import_days) == (500, 1)
    chain_transitions = await db_session.scalar(
        select(func.count()).select_from(StatusTransition).where(StatusTransition.action.in_(CHAIN))
    )
    chain_mails = await db_session.scalar(
        select(func.count())
        .select_from(MailLog)
        .where(
            MailLog.template_id.in_(
                [
                    "sr.invitation",
                    "sr.campus_info",
                    "sr.reminder_1",
                    "sr.reminder_2",
                    "sr.escalation",
                    "lease.info",
                    "lease.info_campus",
                    "lease.reminder_1",
                    "lease.reminder_1_campus",
                    "lease.reminder_2",
                    "lease.reminder_2_campus",
                    "lease.escalation",
                    "lease.escalation_campus",
                ]
            )
        )
    )
    assert chain_mails == chain_transitions
    holds = await db_session.scalar(
        select(func.count())
        .select_from(MailLog)
        .where(MailLog.template_id == "sr.hardware_unavailable")
    )
    assert holds == 10  # every hardware_unavailable SR was notified (SPEC §11.3)
    statuses = Counter((await db_session.scalars(select(MailLog.status))).all())
    assert statuses[MailStatus.FAILED] == 2
    assert statuses[MailStatus.SENT] > 400
    rendered = (
        await db_session.scalars(select(MailLog).where(MailLog.template_id == "sr.reminder_1"))
    ).first()
    assert rendered is not None
    assert rendered.body_html
    assert rendered.subject == f"Reminder: book your appointment for {rendered.entity_label}"


async def test_dispatch_runs_match_job_transitions(db_session: AsyncSession) -> None:
    runs = (
        await db_session.scalars(
            select(DispatchRun)
            .where(DispatchRun.dry_run.is_(False))
            .order_by(DispatchRun.started_at)
        )
    ).all()
    assert 13 <= len(runs) <= 14
    for run in runs:
        assert run.status == JobStatus.SUCCEEDED
        assert run.trigger == Trigger.JOB
        job = await db_session.get(BackgroundJob, run.background_job_id)
        assert job is not None
        assert job.result is not None
        assert job.result["dispatchRunId"] == run.id
    day = to_business_date(runs[-2].started_at)
    job_rows = (
        await db_session.scalars(
            select(StatusTransition).where(
                StatusTransition.trigger == Trigger.JOB, StatusTransition.action.in_(CHAIN)
            )
        )
    ).all()
    sent_that_day = sum(1 for row in job_rows if to_business_date(row.occurred_at) == day)
    assert sum(runs[-2].counts.values()) == sent_that_day
    dry = (await db_session.scalars(select(DispatchRun).where(DispatchRun.dry_run.is_(True)))).all()
    assert len(dry) == 1
    assert dry[0].planned is not None
    assert sum(dry[0].counts.values()) == len(dry[0].planned)


async def test_seeder_is_idempotent_and_exports(db_session: AsyncSession) -> None:
    before = await db_session.scalar(select(func.count()).select_from(MailLog))
    await seed_comms(db_session, SeedContext.create(10))
    assert await db_session.scalar(select(func.count()).select_from(MailLog)) == before
    exported = await export_comms(db_session, SeedContext.create(10))
    assert set(exported) == {"dispatch_settings", "dispatch_runs", "mail_log"}
    mails = exported["mail_log"]
    assert len(mails) == before
    assert "templateId" in mails[0]
    assert sum(1 for m in mails if m["bodyHtml"]) <= EXPORT_HTML_ROWS
    assert all(m["bodyText"] for m in mails if m["templateId"].startswith("lease."))
