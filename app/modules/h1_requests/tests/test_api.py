"""H1 read/write endpoints: lists + filters, detail, PATCH, history, exports, contacts,
pipeline, mail log, dispatch settings, notification templates."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import LeaseReturnStatus, ServiceRequestStatus
from app.core.models import ServiceRequest
from app.core.timeutil import business_today, now_utc
from app.modules.h1_requests.models import MailLog
from app.testing import factories as f

from .conftest import ADA, DANA, THEO, VIC, Headers, make_lease

SR = ServiceRequestStatus
LR = LeaseReturnStatus


async def _get(client: AsyncClient, headers: Headers, url: str, who: str = DANA) -> Any:
    response = await client.get(f"/api/v1{url}", headers=await headers(who))
    assert response.status_code == 200, response.text
    return response.json()


# --- service request list ---------------------------------------------------------------------------


async def test_sr_list_defaults_hide_closed_and_vip(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    body = await _get(client, headers, "/service-requests?perPage=200")
    statuses = {item["status"] for item in body["data"]}
    assert not statuses & {"completed", "cancelled"}
    assert all(not item["isVip"] for item in body["data"])
    total_open = await db_session.scalar(
        select(func.count())
        .select_from(ServiceRequest)
        .where(
            ServiceRequest.status.not_in(("completed", "cancelled")),
            ServiceRequest.is_vip.is_(False),
        )
    )
    assert body["meta"]["total"] == total_open
    vip = await _get(client, headers, "/service-requests?vip=only&perPage=200")
    assert vip["meta"]["total"] >= 6
    assert all(item["isVip"] for item in vip["data"])
    closed = await _get(
        client, headers, "/service-requests?includeClosed=true&stage=deployed,closed"
    )
    assert {item["stage"] for item in closed["data"]} <= {"deployed", "closed"}
    assert closed["meta"]["total"] > 0


async def test_sr_list_filters(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    dana = await f.user(db_session)
    unresolved = await f.make_sr(db_session, status=SR.IMPORTED, region_code=None)
    edited = await f.make_sr(
        db_session,
        status=SR.PERSONALIZATION_IN_PROGRESS,
        editor=dana,
        appointment=now_utc() + timedelta(days=2),
    )
    paused = await f.make_sr(db_session, status=SR.INVITED, communication_paused=True)
    none = await _get(client, headers, "/service-requests?regionId=none&perPage=200")
    assert unresolved.id in {i["id"] for i in none["data"]}
    assert all(i["region"] is None for i in none["data"])
    attention = await _get(
        client, headers, "/service-requests?attention=region_unresolved&perPage=200"
    )
    assert unresolved.id in {i["id"] for i in attention["data"]}
    needs = await _get(client, headers, "/service-requests?needsAttention=true&perPage=200")
    assert all(i["attention"] for i in needs["data"])
    mine = await _get(client, headers, "/service-requests?editorId=me&perPage=200")
    assert edited.id in {i["id"] for i in mine["data"]}
    assert all(i["editor"]["id"] == dana.id for i in mine["data"])
    paused_list = await _get(client, headers, "/service-requests?paused=true&perPage=200")
    assert paused.id in {i["id"] for i in paused_list["data"]}
    by_number = await _get(client, headers, f"/service-requests?q={edited.number[-5:]}")
    assert edited.id in {i["id"] for i in by_number["data"]}
    by_name = await _get(client, headers, f"/service-requests?q={edited.contact.last_name}")
    assert [i["id"] for i in by_name["data"]] == [edited.id]
    ids = await _get(client, headers, f"/service-requests?ids={unresolved.id},{paused.id}")
    assert {i["id"] for i in ids["data"]} == {unresolved.id, paused.id}
    sorted_ = await _get(client, headers, "/service-requests?sort=number&perPage=5")
    numbers = [i["number"] for i in sorted_["data"]]
    assert numbers == sorted(numbers)
    by_region = await _get(client, headers, "/service-requests?sort=region,-number&perPage=5")
    assert by_region["meta"]["total"] > 0


@pytest.mark.parametrize(
    "query",
    [
        "status=bogus",
        "sort=whatever",
        "vip=sometimes",
        "editorId=x1",
        "regionId=abc",
        "appointmentFrom=2026-13-01",
        "importedFrom=2026-10-10&importedTo=2026-10-01",
    ],
)
async def test_sr_list_validation(client: AsyncClient, headers: Headers, query: str) -> None:
    response = await client.get(f"/api/v1/service-requests?{query}", headers=await headers(DANA))
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_sr_read_permissions(client: AsyncClient, headers: Headers) -> None:
    for who in (VIC, THEO, ADA):
        assert (
            await client.get("/api/v1/service-requests", headers=await headers(who))
        ).status_code == 200
    assert (await client.get("/api/v1/service-requests")).status_code == 401
    export = await client.post("/api/v1/service-requests/export", headers=await headers(VIC))
    assert export.status_code == 403


# --- detail / patch / history / export ---------------------------------------------------------------


async def test_detail_by_number_history_and_404(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    sr = await f.make_sr(db_session, status=SR.IMPORTED)
    await client.post(
        f"/api/v1/service-requests/{sr.id}/actions/send_invitation",
        json={},
        headers=await headers(DANA),
    )
    detail = await _get(client, headers, f"/service-requests/by-number/{sr.number.lower()}")
    assert detail["id"] == sr.id
    assert detail["bookingLink"].endswith(f"ref={sr.number}")
    history = await _get(client, headers, f"/service-requests/{sr.id}/history")
    kinds = [entry["kind"] for entry in history["data"]]
    assert kinds[0] in ("mail", "transition")
    assert "mail" in kinds
    transition = next(e for e in history["data"] if e["kind"] == "transition")
    assert transition["action"] == "send_invitation"
    assert transition["actionLabel"] == "Send booking invitation"
    assert transition["id"].startswith("transition:")
    missing = await client.get("/api/v1/service-requests/9999999", headers=await headers(DANA))
    assert missing.status_code == 404


async def test_history_includes_asset_movements(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    sr_id = (
        await db_session.scalars(
            select(ServiceRequest.id).where(ServiceRequest.status == SR.COMPLETED)
        )
    ).first()
    history = await _get(client, headers, f"/service-requests/{sr_id}/history")
    movement = next(e for e in history["data"] if e["kind"] == "asset_movement")
    assert movement["asset"]["serialNumber"]
    assert movement["toLocation"]["code"]


async def test_patch_service_request(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    sr = await f.make_sr(db_session, status=SR.INVITED)
    auth = await headers(DANA)
    url = f"/api/v1/service-requests/{sr.id}"
    moved = await client.patch(
        url,
        json={
            "deliveryAddress": {"zip": "5012", "city": "Westbay", "room": "4.12"},
            "note": " Call first ",
            "version": sr.version,
        },
        headers=auth,
    )
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["deliveryAddress"]["zip"] == "05012"
    assert body["deliveryAddress"]["room"] == "4.12"
    assert body["note"] == "Call first"
    westbay = await client.patch(url, json={"deliveryAddress": {"zip": "50123"}}, headers=auth)
    assert westbay.json()["region"]["code"] == "westbay"
    assert westbay.json()["regionRule"]["matchKind"] == "zip_prefix"
    override = await client.patch(
        url, json={"regionId": (await f.region(db_session, "hillford")).id}, headers=auth
    )
    assert override.json()["region"]["code"] == "hillford"
    assert override.json()["regionOverridden"] is True
    cleared = await client.patch(url, json={"regionId": None}, headers=auth)
    assert cleared.json()["regionOverridden"] is False
    assert cleared.json()["region"]["code"] == "westbay"
    delivery = await client.patch(
        url, json={"deliveryTypeOverride": "parcel_to_user"}, headers=auth
    )
    assert delivery.json()["deliveryType"] == "parcel_to_user"
    stale = await client.patch(url, json={"note": "x", "version": 1}, headers=auth)
    assert stale.status_code == 409
    assert stale.json()["code"] == "version_conflict"
    bad_zip = await client.patch(url, json={"deliveryAddress": {"zip": "12"}}, headers=auth)
    assert bad_zip.status_code == 422
    unknown = await client.patch(url, json={"regionId": 999999}, headers=auth)
    assert unknown.status_code == 422
    extra = await client.patch(url, json={"status": "completed"}, headers=auth)
    assert extra.status_code == 422
    viewer = await client.patch(url, json={"note": "x"}, headers=await headers(VIC))
    assert viewer.status_code == 403


async def test_patch_locks(db_session: AsyncSession, client: AsyncClient, headers: Headers) -> None:
    auth = await headers(DANA)
    booked = await f.make_sr(
        db_session, status=SR.READY_FOR_PICKING, appointment=now_utc() + timedelta(days=1)
    )
    locked = await client.patch(
        f"/api/v1/service-requests/{booked.id}", json={"costCenter": "CC-1234"}, headers=auth
    )
    assert locked.status_code == 409
    assert locked.json()["detail"] == "Region is locked after staging started."
    fine = await client.patch(
        f"/api/v1/service-requests/{booked.id}",
        json={"onsiteContactPhone": "+00 555 0100"},
        headers=auth,
    )
    assert fine.status_code == 200
    done = await f.make_sr(db_session, status=SR.COMPLETED)
    closed = await client.patch(
        f"/api/v1/service-requests/{done.id}", json={"note": "x"}, headers=auth
    )
    assert closed.status_code == 409
    campus = await f.make_sr(db_session, status=SR.IMPORTED, campus=True)
    parcel = await client.patch(
        f"/api/v1/service-requests/{campus.id}",
        json={"deliveryTypeOverride": "parcel_to_user"},
        headers=auth,
    )
    assert parcel.status_code == 422


async def test_exports(client: AsyncClient, headers: Headers) -> None:
    auth = await headers(DANA)
    sr_export = await client.post("/api/v1/service-requests/export?status=escalated", headers=auth)
    assert sr_export.status_code == 200, sr_export.text
    link = sr_export.json()
    assert link["filename"].endswith(".xlsx")
    assert link["sizeBytes"] > 1000
    download = await client.get("/api/v1" + link["url"].split("/api/v1", 1)[1])
    assert download.status_code == 200
    assert download.content[:2] == b"PK"
    lease_export = await client.post("/api/v1/lease-returns/export", headers=auth)
    assert lease_export.status_code == 200
    assert (
        await client.post("/api/v1/lease-returns/export", headers=await headers(THEO))
    ).status_code == 403


# --- lease returns ------------------------------------------------------------------------------------


async def test_lease_list_detail_patch_comments(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    default = await _get(client, headers, "/lease-returns?perPage=200")
    assert not {i["status"] for i in default["data"]} & {"replacement_ordered", "closed"}
    retirement = [i["retirementDate"] for i in default["data"]]
    assert retirement == sorted(retirement)
    linked = await _get(
        client, headers, "/lease-returns?includeClosed=true&status=replacement_ordered&perPage=200"
    )
    assert linked["meta"]["total"] >= 38
    assert all(i["serviceRequest"] for i in linked["data"])
    overdue = await _get(client, headers, "/lease-returns?attention=retirement_passed&perPage=200")
    assert all(i["daysUntilRetirement"] < 0 for i in overdue["data"])
    lease = await make_lease(db_session, retirement_date=business_today() + timedelta(days=12))
    hit = await _get(client, headers, f"/lease-returns?q={lease.asset_tag}")
    assert [i["id"] for i in hit["data"]] == [lease.id]
    detail = await _get(client, headers, f"/lease-returns/{lease.id}")
    assert detail["daysUntilRetirement"] == 12
    auth = await headers(DANA)
    patched = await client.patch(
        f"/api/v1/lease-returns/{lease.id}",
        json={"escalationEmail": "Boss@Example.com"},
        headers=auth,
    )
    assert patched.json()["escalationEmail"] == "boss@example.com"
    invalid = await client.patch(
        f"/api/v1/lease-returns/{lease.id}", json={"escalationEmail": "nope"}, headers=auth
    )
    assert invalid.status_code == 422
    created = await client.post(
        f"/api/v1/lease-returns/{lease.id}/comments", json={"body": "Called the user"}, headers=auth
    )
    assert created.status_code == 201
    assert created.json()["author"]["email"] == DANA
    comments = await _get(client, headers, f"/lease-returns/{lease.id}/comments", who=VIC)
    assert comments["data"][0]["body"] == "Called the user"
    history = await _get(client, headers, f"/lease-returns/{lease.id}/history")
    assert history["data"][0]["kind"] == "comment"
    viewer = await client.post(
        f"/api/v1/lease-returns/{lease.id}/comments", json={"body": "x"}, headers=await headers(VIC)
    )
    assert viewer.status_code == 403
    too_long = await client.post(
        f"/api/v1/lease-returns/{lease.id}/comments", json={"body": "x" * 2001}, headers=auth
    )
    assert too_long.status_code == 422


# --- contacts / pipeline -------------------------------------------------------------------------------


async def test_contacts(db_session: AsyncSession, client: AsyncClient, headers: Headers) -> None:
    listing = await _get(client, headers, "/contacts?perPage=5")
    assert listing["meta"]["total"] >= 420
    names = [i["displayName"].split()[-1] for i in listing["data"]]
    assert names == sorted(names)
    vips = await _get(client, headers, "/contacts?vip=only&perPage=50")
    assert vips["meta"]["total"] == 8
    sr = await f.make_sr(db_session, status=SR.IMPORTED)
    contact = sr.contact
    detail = await _get(client, headers, f"/contacts/{contact.id}")
    assert detail["serviceRequests"][0]["number"] == sr.number
    auth = await headers(DANA)
    patched = await client.patch(
        f"/api/v1/contacts/{contact.id}",
        json={"email": "New.Mail@Example.com", "zip": "1050"},
        headers=auth,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["email"] == "new.mail@example.com"
    assert patched.json()["address"]["zip"] == "01050"
    forbidden = await client.patch(
        f"/api/v1/contacts/{contact.id}", json={"phone": "1"}, headers=await headers(VIC)
    )
    assert forbidden.status_code == 403
    invalid = await client.patch(
        f"/api/v1/contacts/{contact.id}", json={"email": "not-an-email"}, headers=auth
    )
    assert invalid.status_code == 422


async def test_contact_address_updates_lease_campus_flag(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    lease = await make_lease(db_session)
    assert lease.is_campus is False
    response = await client.patch(
        f"/api/v1/contacts/{lease.contact_id}", json={"zip": "10500"}, headers=await headers(DANA)
    )
    assert response.status_code == 200
    detail = await _get(client, headers, f"/lease-returns/{lease.id}")
    assert detail["isCampus"] is True


async def test_pipeline_summary(client: AsyncClient, headers: Headers) -> None:
    summary = await _get(client, headers, "/pipeline/summary", who=VIC)
    stages = [s["stage"] for s in summary["stages"]]
    assert stages == [
        "imported",
        "invited",
        "booked",
        "personalizing",
        "ready",
        "delivery",
        "deployed",
        "closed",
    ]
    imported = summary["stages"][0]
    assert [s["status"] for s in imported["statuses"]] == [
        "imported",
        "imported_vip",
        "hardware_unavailable",
    ]
    assert imported["statuses"][1]["count"] == 0  # vip=exclude
    totals = summary["totals"]
    assert totals["open"] == sum(s["count"] for s in summary["stages"][:6])
    assert totals["vipOpen"] >= 6
    assert totals["needsAttention"] > 0
    assert totals["appointmentsToday"] >= 5
    vip = await _get(client, headers, "/pipeline/summary?vip=include")
    assert vip["stages"][0]["statuses"][1]["count"] == 6
    riverton = await _get(client, headers, "/pipeline/summary?siteId=1")
    assert riverton["totals"]["open"] < totals["open"]
    bad = await client.get(
        "/api/v1/pipeline/summary?deliveryType=drone", headers=await headers(DANA)
    )
    assert bad.status_code == 422


# --- communications -----------------------------------------------------------------------------------


async def test_mail_log_list_and_detail(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    page = await _get(client, headers, "/mail-log?perPage=5")
    assert page["meta"]["total"] > 400
    queued = [i["queuedAt"] for i in page["data"]]
    assert queued == sorted(queued, reverse=True)
    lease_mails = await _get(
        client, headers, "/mail-log?templateId=lease.info,lease.reminder_1&perPage=200"
    )
    assert {i["templateId"] for i in lease_mails["data"]} <= {"lease.info", "lease.reminder_1"}
    failed = await _get(client, headers, "/mail-log?status=failed")
    assert failed["meta"]["total"] == 2
    assert failed["data"][0]["error"]
    first = page["data"][0]
    hit = await _get(client, headers, f"/mail-log?q={first['to'][0]}")
    assert first["id"] in {i["id"] for i in hit["data"]}
    detail = await _get(client, headers, f"/mail-log/{first['id']}")
    assert detail["templateName"]
    protocol = (
        await db_session.scalars(
            select(MailLog.id)
            .join(ServiceRequest, ServiceRequest.id == MailLog.entity_id)
            .where(
                MailLog.template_id == "sr.protocol", ServiceRequest.protocol_file_id.is_not(None)
            )
        )
    ).first()
    if protocol is not None:  # protocol PDFs are seeded by H4 (SPEC_RESOLUTIONS #6)
        with_attachment = await _get(client, headers, f"/mail-log/{protocol}")
        assert with_attachment["attachmentCount"] == 1
        assert with_attachment["attachments"][0]["download"]["url"]
    today = business_today().isoformat()
    ranged = await _get(client, headers, f"/mail-log?from={today}&to={today}&trigger=sync")
    assert all(i["trigger"] == "sync" for i in ranged["data"])
    assert (await client.get("/api/v1/mail-log", headers=await headers(THEO))).status_code == 403
    assert (
        await client.get("/api/v1/mail-log/99999999", headers=await headers(DANA))
    ).status_code == 404


async def test_dispatch_settings(client: AsyncClient, headers: Headers) -> None:
    current = await _get(client, headers, "/dispatch-settings", who=VIC)
    assert current["leaseInfoDaysBeforeRetirement"] == 42
    assert current["nextScheduledRunAt"].endswith("Z")
    payload = {
        k: v
        for k, v in current.items()
        if k not in ("updatedAt", "updatedBy", "nextScheduledRunAt")
    }
    payload["srReminder1AfterDays"] = 3
    auth = await headers(DANA)
    saved = await client.put("/api/v1/dispatch-settings", json=payload, headers=auth)
    assert saved.status_code == 200, saved.text
    assert saved.json()["srReminder1AfterDays"] == 3
    assert saved.json()["updatedBy"]["email"] == DANA
    too_big = await client.put(
        "/api/v1/dispatch-settings", json={**payload, "maxMailsPerRun": 9000}, headers=auth
    )
    assert too_big.status_code == 422
    assert "maxMailsPerRun" in too_big.json()["errors"]
    partial = await client.put(
        "/api/v1/dispatch-settings", json={"srMailsEnabled": False}, headers=auth
    )
    assert partial.status_code == 422
    viewer = await client.put("/api/v1/dispatch-settings", json=payload, headers=await headers(VIC))
    assert viewer.status_code == 403


async def test_notification_templates_and_preview(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    listing = await _get(client, headers, "/notification-templates")
    ids = {t["id"] for t in listing["data"]}
    expected = {
        "sr.invitation", "sr.campus_info", "sr.reminder_1", "sr.reminder_2", "sr.escalation",
        "sr.hardware_unavailable", "sr.reschedule", "sr.reschedule_campus",
        "lease.info", "lease.info_campus", "lease.reminder_1", "lease.reminder_1_campus",
        "lease.reminder_2", "lease.reminder_2_campus", "lease.escalation", "lease.escalation_campus",
    }  # fmt: skip
    assert expected <= ids
    invitation = next(t for t in listing["data"] if t["id"] == "sr.invitation")
    assert invitation["owner"] == "h1"
    assert invitation["sentLast7Days"] > 0
    assert invitation["entityType"] == "service_request"
    for template_id in expected:
        sample = await _get(client, headers, f"/notification-templates/{template_id}/preview")
        assert sample["subject"]
        assert "Acme IT Services" in sample["html"]
    sr = await f.make_sr(db_session, status=SR.INVITED)
    real = await _get(
        client,
        headers,
        f"/notification-templates/sr.reminder_1/preview?entityType=service_request&entityId={sr.id}",
    )
    assert sr.number in real["subject"]
    assert "Dear Test Person" in real["text"]
    wrong = await client.get(
        f"/api/v1/notification-templates/lease.info/preview?entityType=service_request&entityId={sr.id}",
        headers=await headers(DANA),
    )
    assert wrong.status_code == 422
    unknown = await client.get(
        "/api/v1/notification-templates/nope/preview", headers=await headers(DANA)
    )
    assert unknown.status_code == 404
    lease = await make_lease(db_session)
    lease_preview = await _get(
        client, headers, f"/notification-templates/lease.escalation/preview?entityId={lease.id}"
    )
    assert lease.asset_tag in lease_preview["subject"]
