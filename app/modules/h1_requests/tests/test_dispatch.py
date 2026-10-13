"""Daily dispatch (SPEC §6.3 algorithm, §9; SPEC_RESOLUTIONS #10)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import commit
from app.core.enums import JobStatus, LeaseReturnStatus, ServiceRequestStatus, Trigger
from app.core.models import LeaseReturn, ServiceRequest, StatusTransition
from app.core.timeutil import business_datetime, business_today
from app.modules.h1_requests import dispatch
from app.modules.h1_requests.dispatch import execute
from app.modules.h1_requests.models import DispatchRun, DispatchSettings, MailLog
from app.testing import factories as f

from .conftest import DANA, VIC, Headers, make_lease

SR = ServiceRequestStatus
LR = LeaseReturnStatus


async def _quiet_story(session: AsyncSession) -> None:
    """Pause every seeded record so a test only sees its own candidates."""
    for model in (ServiceRequest, LeaseReturn):
        await session.execute(update(model).values(communication_paused=True))
    await session.flush()
    session.expire_all()


async def _due_sr(session: AsyncSession, **fields: Any) -> ServiceRequest:
    yesterday = business_today() - timedelta(days=1)
    return await f.make_sr(
        session, status=SR.IMPORTED, imported_at=business_datetime(yesterday, 9), **fields
    )


async def test_real_run_advances_each_entity_once(db_session: AsyncSession) -> None:
    await _quiet_story(db_session)
    today = business_today()
    first = await _due_sr(db_session)
    campus = await _due_sr(db_session, campus=True)
    reminder = await f.make_sr(
        db_session, status=SR.REMINDER_1_SENT, communication_date=today - timedelta(days=6)
    )
    no_mail = await _due_sr(db_session, email=None)
    lease = await make_lease(db_session, retirement_date=today + timedelta(days=10))
    ids = (first.id, campus.id, reminder.id, no_mail.id, lease.id)
    await commit(db_session)  # setup survives the per-entity rollbacks of the run

    outcome = await execute(db_session, dry_run=False)

    assert dict(outcome.counts) == {
        "sr.invitation": 1,
        "sr.campus_info": 1,
        "sr.reminder_2": 1,
        "lease.info": 1,
    }
    assert dict(outcome.skipped) == {"contact_email": 1}
    assert outcome.errors == []
    statuses = {
        sr.id: sr.status
        for sr in (
            await db_session.scalars(select(ServiceRequest).where(ServiceRequest.id.in_(ids[:4])))
        ).all()
    }
    assert statuses == {
        ids[0]: SR.INVITED,
        ids[1]: SR.CAMPUS_INFO_SENT,
        ids[2]: SR.REMINDER_2_SENT,  # not escalated in the same run (reverse order)
        ids[3]: SR.IMPORTED,
    }
    record = (
        await db_session.scalars(
            select(StatusTransition).where(
                StatusTransition.entity_type == "service_request",
                StatusTransition.entity_id == ids[0],
                StatusTransition.action == "send_invitation",
            )
        )
    ).one()
    assert record.trigger == Trigger.JOB
    mail = (
        await db_session.scalars(
            select(MailLog).where(
                MailLog.entity_id == ids[0], MailLog.entity_type == "service_request"
            )
        )
    ).one()
    assert mail.trigger == Trigger.JOB
    # idempotent: a second run the same day sends nothing new
    again = await execute(db_session, dry_run=False)
    assert again.sent == 0


async def test_master_switch_and_cap(
    db_session: AsyncSession, settings_row: DispatchSettings
) -> None:
    await _quiet_story(db_session)
    for _ in range(3):
        await _due_sr(db_session)
    await make_lease(db_session, retirement_date=business_today() + timedelta(days=3))
    settings_row.lease_mails_enabled = False
    settings_row.max_mails_per_run = 2
    await db_session.flush()
    outcome = await execute(db_session, dry_run=True)
    assert outcome.sent == 2
    assert outcome.skipped["cap_reached"] == 1
    assert "lease.info" not in outcome.counts
    assert outcome.planned is not None
    assert len(outcome.planned) == 2
    assert outcome.planned[0]["templateId"] == "sr.invitation"
    assert outcome.planned[0]["to"] == ["test.person@example.com"]


async def test_dry_run_changes_nothing(db_session: AsyncSession) -> None:
    await _quiet_story(db_session)
    sr = await _due_sr(db_session)
    outcome = await execute(db_session, dry_run=True)
    assert outcome.counts["sr.invitation"] == 1
    await db_session.refresh(sr)
    assert sr.status == SR.IMPORTED
    assert (
        await db_session.scalars(
            select(MailLog).where(
                MailLog.entity_id == sr.id, MailLog.entity_type == "service_request"
            )
        )
    ).all() == []


async def test_errors_are_recorded_and_the_run_continues(db_session: AsyncSession) -> None:
    await _quiet_story(db_session)
    broken = await _due_sr(db_session)
    fine = await _due_sr(db_session)
    broken_id, broken_number = broken.id, broken.number
    await commit(db_session)
    real_apply = dispatch.transition_service.apply

    async def flaky(session: AsyncSession, entity: Any, action: str, **kwargs: Any) -> Any:
        if entity.id == broken_id:
            raise RuntimeError("provider exploded")
        return await real_apply(session, entity, action, **kwargs)

    with patch.object(dispatch.transition_service, "apply", flaky):
        outcome = await execute(db_session, dry_run=False)
    assert outcome.counts["sr.invitation"] == 1
    assert outcome.errors == [
        {
            "entityType": "service_request",
            "entityId": broken_id,
            "label": broken_number,
            "action": "send_invitation",
            "message": "provider exploded",
        }
    ]
    await db_session.refresh(fine)
    assert fine.status == SR.INVITED


async def test_endpoints_dry_run_real_run_and_conflict(
    db_session: AsyncSession, client: AsyncClient, headers: Headers, jobs: InMemoryConnector
) -> None:
    auth = await headers(DANA)
    dry = await client.post("/api/v1/comms/dispatch-runs", json={"dryRun": True}, headers=auth)
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["dryRun"] is True
    assert body["status"] == "succeeded"
    assert body["trigger"] == "user"
    assert body["triggeredBy"]["email"] == DANA
    assert isinstance(body["planned"], list)
    real = await client.post("/api/v1/comms/dispatch-runs", json={}, headers=auth)
    assert real.status_code == 202, real.text
    accepted = real.json()
    assert accepted["dispatchRunId"]
    assert accepted["jobId"]
    [job] = jobs.jobs.values()
    assert job["task_name"] == "comms.dispatch"
    assert job["args"] == {
        "job_id": accepted["jobId"],
        "dispatch_run_id": accepted["dispatchRunId"],
    }
    conflict = await client.post("/api/v1/comms/dispatch-runs", json={}, headers=auth)
    assert conflict.status_code == 409
    listed = await client.get("/api/v1/comms/dispatch-runs?dryRun=false&perPage=5", headers=auth)
    assert listed.json()["data"][0]["id"] == accepted["dispatchRunId"]
    one = await client.get(f"/api/v1/comms/dispatch-runs/{accepted['dispatchRunId']}", headers=auth)
    assert one.json()["status"] == "queued"
    forbidden = await client.post(
        "/api/v1/comms/dispatch-runs", json={"dryRun": True}, headers=await headers(VIC)
    )
    assert forbidden.status_code == 403
    readable = await client.get("/api/v1/comms/dispatch-runs", headers=await headers(VIC))
    assert readable.status_code == 200
    bad = await client.post(
        "/api/v1/comms/dispatch-runs", json={"dryRun": True, "x": 1}, headers=auth
    )
    assert bad.status_code == 422


async def test_periodic_task_only_at_seven(db_session: AsyncSession) -> None:
    calls: list[tuple[int, int]] = []

    async def fake_perform(job_id: int, run_id: int) -> None:
        calls.append((job_id, run_id))

    with (
        patch.object(dispatch, "business_hour", return_value=6),
        patch.object(dispatch, "perform_run", fake_perform),
    ):
        await dispatch.dispatch_task(timestamp=0)
    assert calls == []
    with patch.object(dispatch, "perform_run", fake_perform):
        await dispatch.dispatch_task(job_id=5, dispatch_run_id=9)
    assert calls == [(5, 9)]


async def test_scheduled_run_today_detection(db_session: AsyncSession) -> None:
    from app.core.timeutil import now_utc

    db_session.add(
        DispatchRun(
            trigger=Trigger.JOB,
            dry_run=False,
            status=JobStatus.SUCCEEDED,
            started_at=now_utc(),
            counts={},
            skipped={},
            errors=[],
        )
    )
    await db_session.flush()
    assert await dispatch.scheduled_run_today(db_session)
