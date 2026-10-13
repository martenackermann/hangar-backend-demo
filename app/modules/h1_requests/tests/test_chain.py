"""Communication chain rules (SPEC §6.3): due rule, pause, retirement window — Python
and SQL agree; H1 guards only apply to the job trigger."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import LeaseReturnStatus, ServiceRequestStatus, Trigger
from app.core.errors import GuardFailed
from app.core.models import LeaseReturn, ServiceRequest
from app.core.timeutil import business_datetime, business_today, now_utc
from app.core.transitions import transition_service
from app.modules.h1_requests.chain import (
    LEASE_STEPS,
    SR_STEPS,
    candidates_stmt,
    due_date,
    is_due,
    is_paused,
    next_scheduled_run,
    next_step,
    template_for,
)
from app.modules.h1_requests.models import DispatchSettings
from app.testing import factories as f

from .conftest import make_lease

SR = ServiceRequestStatus
LR = LeaseReturnStatus


async def _in_sql(session: AsyncSession, entity: object, settings: DispatchSettings) -> bool:
    entity_type = "service_request" if isinstance(entity, ServiceRequest) else "lease_return"
    step = next_step(entity_type, entity)
    assert step is not None
    stmt = candidates_stmt(step, settings, business_today(), after_id=0, limit=10_000)
    model = ServiceRequest if entity_type == "service_request" else LeaseReturn
    ids = (await session.scalars(stmt.with_only_columns(model.id))).all()
    return entity.id in ids  # type: ignore[attr-defined]


async def test_sr_reminder_due_after_n_days(
    db_session: AsyncSession, settings_row: DispatchSettings
) -> None:
    today = business_today()
    due = await f.make_sr(
        db_session, status=SR.INVITED, communication_date=today - timedelta(days=5)
    )
    early = await f.make_sr(
        db_session, status=SR.INVITED, communication_date=today - timedelta(days=4)
    )
    step = next_step("service_request", due)
    assert step is not None
    assert step.action == "send_reminder_1"
    assert is_due(due, step, settings_row, today)
    assert not is_due(early, step, settings_row, today)
    assert due_date(early, step, settings_row) == today + timedelta(days=1)
    assert await _in_sql(db_session, due, settings_row)
    assert not await _in_sql(db_session, early, settings_row)


async def test_resume_date_moves_the_anchor(
    db_session: AsyncSession, settings_row: DispatchSettings
) -> None:
    today = business_today()
    sr = await f.make_sr(
        db_session,
        status=SR.INVITED,
        communication_date=today - timedelta(days=20),
        communication_resume_date=today - timedelta(days=2),
    )
    step = next_step("service_request", sr)
    assert step is not None
    assert not is_due(sr, step, settings_row, today)  # anchor = resume date (−2) + 5
    assert due_date(sr, step, settings_row) == today + timedelta(days=3)
    assert not await _in_sql(db_session, sr, settings_row)


async def test_first_mail_anchor_is_import_date(
    db_session: AsyncSession, settings_row: DispatchSettings
) -> None:
    today = business_today()
    yesterday = await f.make_sr(
        db_session, status=SR.IMPORTED, imported_at=business_datetime(today - timedelta(days=1), 9)
    )
    just_now = await f.make_sr(db_session, status=SR.IMPORTED, imported_at=now_utc())
    step = SR_STEPS[0]
    assert is_due(yesterday, step, settings_row, today)
    assert not is_due(just_now, step, settings_row, today)
    assert await _in_sql(db_session, yesterday, settings_row)
    assert not await _in_sql(db_session, just_now, settings_row)


async def test_lease_info_window(db_session: AsyncSession, settings_row: DispatchSettings) -> None:
    today = business_today()
    inside = await make_lease(db_session, retirement_date=today + timedelta(days=42))
    outside = await make_lease(db_session, retirement_date=today + timedelta(days=43))
    step = LEASE_STEPS[0]
    assert is_due(inside, step, settings_row, today)
    assert not is_due(outside, step, settings_row, today)
    assert await _in_sql(db_session, inside, settings_row)
    assert not await _in_sql(db_session, outside, settings_row)


async def test_paused_entities_are_not_candidates(
    db_session: AsyncSession, settings_row: DispatchSettings
) -> None:
    today = business_today()
    paused = await f.make_sr(
        db_session,
        status=SR.INVITED,
        communication_date=today - timedelta(days=9),
        communication_paused=True,
    )
    assert is_paused(paused)
    assert not await _in_sql(db_session, paused, settings_row)


async def test_h1_guards_apply_to_job_trigger_only(db_session: AsyncSession) -> None:
    today = business_today()
    dana = await f.user(db_session)
    early = await f.make_sr(db_session, status=SR.INVITED, communication_date=today)
    with pytest.raises(GuardFailed) as caught:
        await transition_service.apply(
            db_session, early, "send_reminder_1", actor=None, trigger=Trigger.JOB
        )
    assert caught.value.guard_code == "chain_due"
    # manual sends ignore the due rule (SPEC §6.3 "Manual sends")
    await transition_service.apply(
        db_session, early, "send_reminder_1", actor=dana, trigger=Trigger.USER
    )
    assert early.status == SR.REMINDER_1_SENT

    paused = await f.make_sr(
        db_session,
        status=SR.INVITED,
        communication_date=today - timedelta(days=9),
        communication_paused=True,
    )
    with pytest.raises(GuardFailed) as caught:
        await transition_service.apply(
            db_session, paused, "send_reminder_1", actor=None, trigger=Trigger.JOB
        )
    assert caught.value.guard_code == "not_paused"

    far = await make_lease(db_session, retirement_date=today + timedelta(days=200))
    with pytest.raises(GuardFailed) as caught:
        await transition_service.apply(
            db_session, far, "send_info", actor=None, trigger=Trigger.JOB
        )
    assert caught.value.guard_code == "retirement_window"


def test_template_ids_and_schedule() -> None:
    assert template_for("lease_return", "send_info", is_campus=True) == "lease.info_campus"
    assert template_for("lease_return", "escalate", is_campus=False) == "lease.escalation"
    assert template_for("service_request", "send_campus_info", is_campus=True) == "sr.campus_info"
    run = next_scheduled_run()
    assert run > now_utc()
    assert run - now_utc() <= timedelta(days=1, hours=1)


async def test_lease_candidates_ignore_other_statuses(
    db_session: AsyncSession, settings_row: DispatchSettings
) -> None:
    today = business_today()
    vip = await make_lease(
        db_session, status=LR.IMPORTED_VIP, retirement_date=today + timedelta(days=5)
    )
    step = LEASE_STEPS[0]
    stmt = candidates_stmt(step, settings_row, today, after_id=0, limit=10_000)
    ids = (await db_session.scalars(stmt.with_only_columns(LeaseReturn.id))).all()
    assert vip.id not in ids
    rows = (
        await db_session.scalars(select(LeaseReturn.status).where(LeaseReturn.id.in_(ids)))
    ).all()
    assert set(rows) <= {LR.IMPORTED}
