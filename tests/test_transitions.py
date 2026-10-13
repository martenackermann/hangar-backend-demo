"""Table-driven tests generated from the transition maps (SPEC §12 "Tests (backend)").

Every allowed (status, action) succeeds with a fixture satisfying the core guards; every
other (status, action) raises ``409 transition_not_allowed`` carrying ``allowedActions``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import LeaseReturnStatus, ServiceRequestStatus, Trigger
from app.core.errors import GuardFailed, TransitionNotAllowed
from app.core.models import ServiceRequest, StatusTransition
from app.core.statemachine import NEW, Transition
from app.core.timeutil import now_utc
from app.core.transitions import LEASE_TRANSITIONS, SR_TRANSITIONS, transition_service
from app.testing import factories as f

SR = ServiceRequestStatus
LR = LeaseReturnStatus


def _cases(tmap: Any) -> list[tuple[str, str]]:
    return [(t.action, status) for t in tmap for status in sorted(t.from_) if t.resolve is None]


def _trigger(t: Transition) -> Trigger:
    return Trigger.USER if Trigger.USER in t.triggers else sorted(t.triggers)[0]


async def _sr_for(session: AsyncSession, action: str, status: str) -> tuple[ServiceRequest, Any]:
    dana = await f.user(session)
    ada = await f.user(session, "ada.admin@example.com")
    future = now_utc() + timedelta(days=3)
    kwargs: dict[str, Any] = {"status": None if status == NEW else SR(status)}
    if action == "send_campus_info":
        kwargs["campus"] = True
    if action in (
        "claim",
        "release",
        "finish_personalization",
        "skip_preparation",
        "close_order",
        "reopen_personalization",
        "book_appointment",
        "request_reschedule",
    ):
        kwargs["appointment"] = future
    if action in ("finish_personalization", "skip_preparation", "release"):
        kwargs["editor"] = dana
    if action == "claim" and status == SR.PERSONALIZATION_IN_PROGRESS:
        kwargs["editor"] = ada  # take-over
    if action == "skip_preparation" and status == SR.APPOINTMENT_BOOKED:
        kwargs["new_types"] = ()
    if action in ("finish_personalization", "skip_preparation", "close_order", "ship"):
        kwargs["assign_new"] = True
        kwargs["packed"] = action == "ship"
    if action in ("ship", "close_without_label"):
        kwargs["region_code"] = "westbay"
    if action == "resume_communication":
        kwargs["communication_paused"] = True
    if action == "send_next_mail" and status == SR.IMPORTED:
        kwargs["campus"] = False
    sr = await f.make_sr(session, **kwargs)
    return sr, dana


@pytest.mark.parametrize(("action", "status"), _cases(SR_TRANSITIONS))
async def test_sr_allowed_transitions(db_session: AsyncSession, action: str, status: str) -> None:
    transition = SR_TRANSITIONS.get(action)
    assert transition is not None
    sr, dana = await _sr_for(db_session, action, status)
    trigger = _trigger(transition)
    actor = dana if trigger == Trigger.USER else None
    result = await transition_service.apply(
        db_session, sr, action, actor=actor, trigger=trigger, comment="test"
    )
    expected = transition.target(sr, None if status == NEW else status)
    assert sr.status == expected == result.to_status
    record = await db_session.get(StatusTransition, result.record.id)
    assert record is not None
    assert record.entity_type == "service_request"
    assert record.from_status == (None if status == NEW else status)
    assert record.trigger == trigger


@pytest.mark.parametrize(("action", "status"), _cases(LEASE_TRANSITIONS))
async def test_lease_allowed_transitions(
    db_session: AsyncSession, action: str, status: str
) -> None:
    transition = LEASE_TRANSITIONS.get(action)
    assert transition is not None
    dana = await f.user(db_session)
    kwargs: dict[str, Any] = {"status": None if status == NEW else LR(status)}
    if action == "resume_communication":
        kwargs["communication_paused"] = True
    if status == LR.REPLACEMENT_ORDERED or action == "link_service_request":
        kwargs["sr"] = await f.make_sr(db_session)
    lease = await f.make_lease(db_session, **kwargs)
    trigger = _trigger(transition)
    result = await transition_service.apply(
        db_session,
        lease,
        action,
        actor=dana if trigger == Trigger.USER else None,
        trigger=trigger,
        comment="test",
    )
    assert lease.status == result.to_status


SR_USER_ACTIONS = [t.action for t in SR_TRANSITIONS if not t.internal]
LEASE_USER_ACTIONS = [t.action for t in LEASE_TRANSITIONS if not t.internal]


@pytest.mark.parametrize("status", [s.value for s in SR])
async def test_sr_forbidden_transitions(db_session: AsyncSession, status: str) -> None:
    dana = await f.user(db_session)
    sr = await f.make_sr(db_session, status=SR(status))
    checked = 0
    for action in SR_USER_ACTIONS:
        transition = SR_TRANSITIONS.get(action)
        assert transition is not None
        if transition.allows_from(status):
            continue
        with pytest.raises(TransitionNotAllowed) as caught:
            await transition_service.apply(db_session, sr, action, actor=dana, trigger=Trigger.USER)
        error = caught.value
        assert error.status == 409
        assert error.code == "transition_not_allowed"
        assert error.extensions["allowedActions"] == transition_service.allowed_actions(sr, dana)
        checked += 1
    assert sr.status == status
    assert checked > 0


@pytest.mark.parametrize("status", [s.value for s in LR])
async def test_lease_forbidden_transitions(db_session: AsyncSession, status: str) -> None:
    dana = await f.user(db_session)
    lease = await f.make_lease(db_session, status=LR(status))
    for action in LEASE_USER_ACTIONS:
        transition = LEASE_TRANSITIONS.get(action)
        assert transition is not None
        if transition.allows_from(status):
            continue
        with pytest.raises(TransitionNotAllowed):
            await transition_service.apply(
                db_session, lease, action, actor=dana, trigger=Trigger.USER
            )


async def test_send_next_mail_resolves_and_records_the_concrete_action(
    db_session: AsyncSession,
) -> None:
    dana = await f.user(db_session)
    sr = await f.make_sr(db_session, status=SR.INVITED)
    result = await transition_service.apply(
        db_session, sr, "send_next_mail", actor=dana, trigger=Trigger.USER
    )
    assert result.action == "send_reminder_1"
    assert result.requested_action == "send_next_mail"
    assert sr.status == SR.REMINDER_1_SENT
    assert sr.communication_date is not None  # SPEC_RESOLUTIONS #9


async def test_guard_failure_is_409_guard_failed(db_session: AsyncSession) -> None:
    dana = await f.user(db_session)
    sr = await f.make_sr(db_session, status=SR.IMPORTED, email=None)
    with pytest.raises(GuardFailed) as caught:
        await transition_service.apply(
            db_session, sr, "send_invitation", actor=dana, trigger=Trigger.USER
        )
    assert caught.value.code == "guard_failed"
    assert caught.value.extensions["guard"]["code"] == "contact_email"
    assert sr.status == SR.IMPORTED


async def test_internal_actions_are_not_user_actions(db_session: AsyncSession) -> None:
    dana = await f.user(db_session)
    sr = await f.make_sr(db_session, status=SR.INVITED)
    with pytest.raises(TransitionNotAllowed):
        await transition_service.apply(
            db_session, sr, "appointment_synced", actor=dana, trigger=Trigger.USER
        )


async def test_permission_is_checked_for_user_trigger(db_session: AsyncSession) -> None:
    from app.core.errors import ForbiddenError

    viewer = await f.user(db_session, "vic.viewer@example.com")
    sr = await f.make_sr(db_session, status=SR.IMPORTED)
    with pytest.raises(ForbiddenError):
        await transition_service.apply(
            db_session, sr, "send_invitation", actor=viewer, trigger=Trigger.USER
        )


async def test_lease_unlink_restores_previous_chain_status(db_session: AsyncSession) -> None:
    dana = await f.user(db_session)
    sr = await f.make_sr(db_session)
    lease = await f.make_lease(
        db_session, status=LR.REPLACEMENT_ORDERED, sr=sr, reminder_1_sent_at=now_utc()
    )
    await transition_service.apply(
        db_session, lease, "unlink_service_request", actor=dana, trigger=Trigger.USER
    )
    assert lease.status == LR.REMINDER_1_SENT
