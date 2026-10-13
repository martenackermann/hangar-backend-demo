"""H1 actions through the generic endpoints: effects, mails, guards, 403/409/422."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    AssetStatus,
    HoldSource,
    LeaseReturnStatus,
    MailStatus,
    ServiceRequestStatus,
    Trigger,
)
from app.core.models import Asset, LeaseReturn, ServiceRequest
from app.core.timeutil import business_today, now_utc
from app.core.transitions import transition_service
from app.modules.h1_requests.models import DispatchSettings, MailLog
from app.testing import factories as f

from .conftest import DANA, THEO, VIC, Headers, make_lease

SR = ServiceRequestStatus
LR = LeaseReturnStatus


async def _post(
    client: AsyncClient,
    headers: Headers,
    url: str,
    body: dict[str, Any] | None = None,
    who: str = DANA,
) -> Any:
    return await client.post(f"/api/v1{url}", json=body or {}, headers=await headers(who))


async def _mails(session: AsyncSession, entity_type: str, entity_id: int) -> list[MailLog]:
    return list(
        (
            await session.scalars(
                select(MailLog)
                .where(MailLog.entity_type == entity_type, MailLog.entity_id == entity_id)
                .order_by(MailLog.id)
            )
        ).all()
    )


# --- chain mails -----------------------------------------------------------------------------------


async def test_send_next_mail_walks_the_sr_chain(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    sr = await f.make_sr(db_session, status=SR.IMPORTED)
    expected = [
        ("invited", "sr.invitation"),
        ("reminder_1_sent", "sr.reminder_1"),
        ("reminder_2_sent", "sr.reminder_2"),
        ("escalated", "sr.escalation"),
    ]
    for status, template in expected:
        response = await _post(client, headers, f"/service-requests/{sr.id}/actions/send_next_mail")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == status
        assert body["communicationDate"] == business_today().isoformat()
        mails = await _mails(db_session, "service_request", sr.id)
        assert mails[-1].template_id == template
        assert mails[-1].status == MailStatus.QUEUED
        assert mails[-1].trigger == Trigger.USER
    escalation = (await _mails(db_session, "service_request", sr.id))[-1]
    assert escalation.cc_addresses == ["fleet-escalations@example.com"]
    done = await _post(client, headers, f"/service-requests/{sr.id}/actions/send_next_mail")
    assert done.status_code == 409
    assert done.json()["code"] == "transition_not_allowed"


async def test_campus_first_mail_and_hidden_variant(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    campus = await f.make_sr(db_session, status=SR.IMPORTED, campus=True)
    standard = await f.make_sr(db_session, status=SR.IMPORTED)
    detail = (
        await client.get(f"/api/v1/service-requests/{campus.id}", headers=await headers(DANA))
    ).json()
    assert "send_campus_info" in detail["availableActions"]
    assert "send_invitation" not in detail["availableActions"]
    assert all(b["action"] != "send_invitation" for b in detail["blockedActions"])
    assert detail["bookingLink"] is None
    other = (
        await client.get(f"/api/v1/service-requests/{standard.id}", headers=await headers(DANA))
    ).json()
    assert "send_invitation" in other["availableActions"]
    assert "send_campus_info" not in other["availableActions"]
    wrong = await _post(client, headers, f"/service-requests/{campus.id}/actions/send_invitation")
    assert wrong.status_code == 403
    assert wrong.json()["code"] == "not_campus"
    ok = await _post(client, headers, f"/service-requests/{campus.id}/actions/send_next_mail")
    assert ok.json()["status"] == "campus_info_sent"
    assert (await _mails(db_session, "service_request", campus.id))[
        -1
    ].template_id == "sr.campus_info"


async def test_guards_block_manual_sends(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    no_mail = await f.make_sr(db_session, status=SR.IMPORTED, email=None)
    detail = (
        await client.get(f"/api/v1/service-requests/{no_mail.id}", headers=await headers(DANA))
    ).json()
    assert {(b["action"], b["guard"]) for b in detail["blockedActions"]} >= {
        ("send_invitation", "contact_email"),
        ("send_next_mail", "contact_email"),
    }
    response = await _post(
        client, headers, f"/service-requests/{no_mail.id}/actions/send_next_mail"
    )
    assert response.status_code == 409
    assert response.json()["guard"]["code"] == "contact_email"
    unresolved = await f.make_sr(db_session, status=SR.IMPORTED, region_code=None)
    response = await _post(
        client, headers, f"/service-requests/{unresolved.id}/actions/send_invitation"
    )
    assert response.json()["guard"]["code"] == "region_resolved"


async def test_lease_chain_with_campus_templates_and_escalation_cc(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    lease = await make_lease(db_session, is_campus=True, escalation_email="Lead@Example.com")
    response: Any = None
    statuses = ["info_sent", "reminder_1_sent", "reminder_2_sent", "escalated"]
    for status in statuses:
        response = await _post(client, headers, f"/lease-returns/{lease.id}/actions/send_next_mail")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == status
    detail = response.json()
    assert detail["infoSentAt"]
    assert detail["reminder1SentAt"]
    assert detail["escalationSentAt"]
    mails = await _mails(db_session, "lease_return", lease.id)
    assert [m.template_id for m in mails] == [
        "lease.info_campus",
        "lease.reminder_1_campus",
        "lease.reminder_2_campus",
        "lease.escalation_campus",
    ]
    assert mails[-1].cc_addresses == ["Lead@Example.com", "fleet-escalations@example.com"]


# --- pause / resume ---------------------------------------------------------------------------------


async def test_pause_and_resume(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    sr = await f.make_sr(db_session, status=SR.INVITED)
    url = f"/service-requests/{sr.id}/actions"
    future = (business_today() + timedelta(days=5)).isoformat()
    paused = await _post(
        client, headers, f"{url}/pause_communication", {"reason": "Leave", "resumeDate": future}
    )
    assert paused.status_code == 200, paused.text
    body = paused.json()
    assert body["communicationPaused"] is False
    assert body["communicationResumeDate"] == future
    assert body["communicationPauseReason"] == "Leave"
    assert "resume_communication" in body["availableActions"]
    indefinite = await _post(
        client, headers, f"{url}/pause_communication", {"reason": "Sick leave"}
    )
    assert indefinite.json()["communicationPaused"] is True
    resumed = await _post(client, headers, f"{url}/resume_communication")
    body = resumed.json()
    assert body["communicationPaused"] is False
    assert body["communicationResumeDate"] == business_today().isoformat()
    assert body["communicationPauseReason"] is None
    assert "resume_communication" not in body["availableActions"]
    blank = await _post(client, headers, f"{url}/pause_communication", {"reason": "   "})
    assert blank.status_code == 422
    assert "reason" in blank.json()["errors"]
    past = await _post(
        client, headers, f"{url}/pause_communication", {"reason": "x", "resumeDate": "2020-01-01"}
    )
    assert past.status_code == 422
    assert "resumeDate" in past.json()["errors"]


async def test_bulk_pause_on_leases(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    a = await make_lease(db_session)
    b = await make_lease(db_session, status=LR.CLOSED)
    a_id, b_id = a.id, b.id
    response = await client.post(
        "/api/v1/lease-returns/bulk/pause_communication",
        json={"ids": [a_id, b_id], "reason": "Project freeze"},
        headers=await headers(DANA),
    )
    assert response.status_code == 200
    assert response.json()["succeeded"] == [a_id]
    assert response.json()["failed"][0]["code"] == "transition_not_allowed"


# --- hardware hold / VIP ----------------------------------------------------------------------------


async def test_hardware_hold_mail_once_and_release_restarts_chain(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    sr = await f.make_sr(db_session, status=SR.INVITED, communication_date=business_today())
    url = f"/service-requests/{sr.id}/actions"
    held = (await _post(client, headers, f"{url}/mark_hardware_unavailable")).json()
    assert held["status"] == "hardware_unavailable"
    assert held["holdSource"] == HoldSource.MANUAL
    assert held["hardwareUnavailableNotifiedAt"] is not None
    released = (await _post(client, headers, f"{url}/release_hardware_hold")).json()
    assert released["status"] == "imported"
    assert released["communicationDate"] is None
    assert released["communicationResumeDate"] == business_today().isoformat()
    assert released["holdSource"] is None
    await _post(client, headers, f"{url}/mark_hardware_unavailable")
    templates = [m.template_id for m in await _mails(db_session, "service_request", sr.id)]
    assert templates.count("sr.hardware_unavailable") == 1  # once per SR


async def test_hold_mail_respects_setting_and_import_source(
    db_session: AsyncSession, settings_row: DispatchSettings
) -> None:
    settings_row.hardware_unavailable_mail_enabled = False
    await db_session.flush()
    sr = await f.make_sr(db_session, status=SR.IMPORTED)
    await transition_service.apply(
        db_session, sr, "mark_hardware_unavailable", actor=None, trigger=Trigger.IMPORT
    )
    assert sr.hold_source == HoldSource.IMPORT
    assert sr.hardware_unavailable_notified_at is None
    assert await _mails(db_session, "service_request", sr.id) == []


async def test_release_vip(db_session: AsyncSession, client: AsyncClient, headers: Headers) -> None:
    sr = await f.make_sr(db_session, status=SR.IMPORTED_VIP, is_vip=True)
    response = await _post(client, headers, f"/service-requests/{sr.id}/actions/release_vip")
    assert response.json()["status"] == "imported"
    lease = await make_lease(db_session, status=LR.IMPORTED_VIP, is_vip=True)
    response = await _post(client, headers, f"/lease-returns/{lease.id}/actions/release_vip")
    assert response.json()["status"] == "imported"


# --- cancel / reopen --------------------------------------------------------------------------------


async def test_cancel_releases_new_assets_and_unlinks_leases(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    dana = await f.user(db_session)
    sr = await f.make_sr(
        db_session,
        status=SR.PERSONALIZATION_IN_PROGRESS,
        appointment=now_utc() + timedelta(days=3),
        editor=dana,
        assign_new=True,
        old_tags=("TZ900001",),
    )
    old = sr.old_items[0]
    lease = await make_lease(db_session, status=LR.REPLACEMENT_ORDERED, sr=sr)
    asset_ids = [i.asset_id for i in sr.new_items]
    sr_id, lease_id = sr.id, lease.id
    response = await _post(
        client, headers, f"/service-requests/{sr_id}/actions/cancel", {"comment": "Duplicate"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["editor"] is None
    assert body["milestones"]["cancelledAt"] is not None
    assert all(item["asset"] is None for item in body["items"] if item["direction"] == "deliver")
    assert old.expected_asset_tag == "TZ900001"
    for asset_id in asset_ids:
        asset = await db_session.get(Asset, asset_id, populate_existing=True)
        assert asset is not None
        assert asset.status == AssetStatus.AVAILABLE
        assert asset.service_request_id is None
    refreshed = await db_session.get(LeaseReturn, lease_id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.status == LR.IMPORTED
    assert refreshed.service_request_id is None
    reopened = (await _post(client, headers, f"/service-requests/{sr_id}/actions/reopen")).json()
    assert reopened["status"] == "imported"
    assert reopened["milestones"]["cancelledAt"] is None
    assert reopened["communicationDate"] is None
    assert reopened["communicationResumeDate"] == business_today().isoformat()


async def test_cancel_on_import_unlinks_too(db_session: AsyncSession) -> None:
    sr = await f.make_sr(db_session, status=SR.INVITED)
    lease = await make_lease(
        db_session, status=LR.REPLACEMENT_ORDERED, sr=sr, info_sent_at=now_utc()
    )
    await transition_service.apply(
        db_session, sr, "cancel", actor=None, trigger=Trigger.IMPORT, comment="Cancelled in ITSM"
    )
    assert lease.status == LR.INFO_SENT
    assert lease.service_request_id is None


# --- lease link / close -----------------------------------------------------------------------------


async def test_lease_link_unlink_close_reopen(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    sr = await f.make_sr(db_session, status=SR.INVITED)
    cancelled = await f.make_sr(db_session, status=SR.CANCELLED)
    lease = await make_lease(db_session)
    url = f"/lease-returns/{lease.id}/actions"
    bad = await _post(
        client, headers, f"{url}/link_service_request", {"serviceRequestNumber": "nope"}
    )
    assert bad.status_code == 422
    missing = await _post(
        client, headers, f"{url}/link_service_request", {"serviceRequestNumber": "SR0999999"}
    )
    assert missing.status_code == 422
    assert "serviceRequestNumber" in missing.json()["errors"]
    dead = await _post(
        client, headers, f"{url}/link_service_request", {"serviceRequestNumber": cancelled.number}
    )
    assert dead.status_code == 422
    linked = await _post(
        client, headers, f"{url}/link_service_request", {"serviceRequestNumber": sr.number.lower()}
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["status"] == "replacement_ordered"
    assert linked.json()["serviceRequest"]["number"] == sr.number
    again = await _post(
        client, headers, f"{url}/link_service_request", {"serviceRequestNumber": sr.number}
    )
    assert again.status_code == 409
    unlinked = await _post(client, headers, f"{url}/unlink_service_request")
    assert unlinked.json()["status"] == "imported"
    assert unlinked.json()["serviceRequest"] is None
    no_comment = await _post(client, headers, f"{url}/close")
    assert no_comment.status_code == 422
    closed = await _post(client, headers, f"{url}/close", {"comment": "Returned at the desk"})
    assert closed.json()["status"] == "closed"
    assert closed.json()["closeReason"] == "Returned at the desk"
    assert closed.json()["closedAt"] is not None
    reopened = await _post(client, headers, f"{url}/reopen")
    assert reopened.json()["status"] == "imported"
    assert reopened.json()["closeReason"] is None


async def test_system_close_uses_reason(db_session: AsyncSession) -> None:
    lease = await make_lease(
        db_session, status=LR.REPLACEMENT_ORDERED, sr=await f.make_sr(db_session)
    )
    await transition_service.apply(
        db_session, lease, "close", actor=None, trigger=Trigger.SYSTEM, comment="device_collected"
    )
    assert lease.close_reason == "device_collected"
    assert lease.closed_at is not None


async def test_close_keeps_reason_set_by_caller(db_session: AsyncSession) -> None:
    """H4 collection sets `close_reason` itself and closes with a human-readable comment."""
    lease = await make_lease(
        db_session, status=LR.REPLACEMENT_ORDERED, sr=await f.make_sr(db_session)
    )
    lease.close_reason = "device_collected"
    await transition_service.apply(
        db_session,
        lease,
        "close",
        actor=None,
        trigger=Trigger.SYSTEM,
        comment="Device collected on site (SR0012345).",
    )
    assert lease.close_reason == "device_collected"
    assert lease.closed_at is not None


async def test_system_close_reason_from_payload(db_session: AsyncSession) -> None:
    """H5 deactivation: SYSTEM close with `closeReason` in the payload (no comment)."""
    lease = await make_lease(db_session, status=LR.INFO_SENT)
    await transition_service.apply(
        db_session,
        lease,
        "close",
        actor=None,
        trigger=Trigger.SYSTEM,
        payload={"closeReason": "device_deactivated"},
    )
    assert lease.status == LR.CLOSED
    assert lease.close_reason == "device_deactivated"
    assert lease.closed_at is not None


# --- reschedule mails -------------------------------------------------------------------------------


async def test_reschedule_mails(db_session: AsyncSession, settings_row: DispatchSettings) -> None:
    future = now_utc() + timedelta(days=2)
    sr = await f.make_sr(db_session, status=SR.APPOINTMENT_BOOKED, appointment=future)
    await transition_service.apply(
        db_session, sr, "appointment_cancelled", actor=None, trigger=Trigger.SYNC
    )
    mails = await _mails(db_session, "service_request", sr.id)
    assert [m.template_id for m in mails] == ["sr.reschedule"]
    assert mails[0].trigger == Trigger.SYNC

    campus = await f.make_sr(db_session, status=SR.PROVIDED, appointment=future, campus=True)
    dana = await f.user(db_session)
    await transition_service.apply(
        db_session,
        campus,
        "request_reschedule",
        actor=dana,
        trigger=Trigger.USER,
        payload={"reason": "x"},
    )
    assert [m.template_id for m in await _mails(db_session, "service_request", campus.id)] == [
        "sr.reschedule_campus"
    ]

    quiet = await f.make_sr(db_session, status=SR.APPOINTMENT_BOOKED, appointment=future)
    await transition_service.apply(
        db_session,
        quiet,
        "request_reschedule",
        actor=dana,
        trigger=Trigger.USER,
        payload={"reason": "x", "notifyUser": False},
    )
    assert await _mails(db_session, "service_request", quiet.id) == []

    settings_row.reschedule_mail_enabled = False  # SPEC_RESOLUTIONS #12
    await db_session.flush()
    off = await f.make_sr(db_session, status=SR.APPOINTMENT_BOOKED, appointment=future)
    await transition_service.apply(
        db_session, off, "appointment_cancelled", actor=None, trigger=Trigger.SYNC
    )
    assert await _mails(db_session, "service_request", off.id) == []


# --- permissions ------------------------------------------------------------------------------------


async def test_roles_and_permissions(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    sr = await f.make_sr(db_session, status=SR.IMPORTED)
    lease = await make_lease(db_session)
    for who in (VIC, THEO):
        detail = (
            await client.get(f"/api/v1/service-requests/{sr.id}", headers=await headers(who))
        ).json()
        assert detail["availableActions"] == []
        assert detail["blockedActions"] == []
        forbidden = await _post(
            client, headers, f"/service-requests/{sr.id}/actions/send_next_mail", who=who
        )
        assert forbidden.status_code == 403
        forbidden = await _post(
            client, headers, f"/lease-returns/{lease.id}/actions/send_info", who=who
        )
        assert forbidden.status_code == 403
    unauthenticated = await client.post(
        f"/api/v1/service-requests/{sr.id}/actions/send_next_mail", json={}
    )
    assert unauthenticated.status_code == 401


async def test_mail_log_follows_notification_events(db_session: AsyncSession) -> None:
    from datetime import UTC, datetime

    from app.core.events import NotificationFailed, NotificationSent, emit

    sr = await f.make_sr(db_session, status=SR.IMPORTED)
    dana = await f.user(db_session)
    await transition_service.apply(
        db_session, sr, "send_invitation", actor=dana, trigger=Trigger.USER
    )
    [mail] = await _mails(db_session, "service_request", sr.id)
    assert mail.triggered_by == dana.id
    assert mail.entity_label == sr.number
    await emit(
        db_session,
        NotificationSent(
            mail.notification_id, "Subject", "<p>x</p>", "x", "<m@example.com>", datetime.now(UTC)
        ),
    )
    await db_session.refresh(mail)
    assert (mail.status, mail.subject, mail.message_id) == (
        MailStatus.SENT,
        "Subject",
        "<m@example.com>",
    )
    await emit(db_session, NotificationFailed(mail.notification_id, "SMTP down"))
    await db_session.refresh(mail)
    assert (mail.status, mail.error) == (MailStatus.FAILED, "SMTP down")
    stored = await db_session.get(ServiceRequest, sr.id)
    assert stored is not None
