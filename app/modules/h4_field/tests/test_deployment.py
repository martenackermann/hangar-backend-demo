"""Deployment scans, verification, unknown devices and SR #26–#29 (SPEC §6.11)."""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable
from datetime import timedelta

from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    AssetSource,
    AssetStatus,
    LeaseReturnStatus,
    ServiceRequestStatus,
    TourStatus,
    TourStopOutcome,
)
from app.core.files import file_store
from app.core.models import Asset, StoredFile, User
from app.core.read_models import build_sr_detail
from app.testing import factories as f

from .conftest import DANA, TARA, THEO, VIC, MakeStop, deferred

SR = ServiceRequestStatus
Headers = Callable[[str], Awaitable[dict[str, str]]]


async def _scan(client: AsyncClient, hdr: dict[str, str], sr_id: int, serial: str) -> dict:
    response = await client.post(
        "/api/v1/scan/deployment", json={"serviceRequestId": sr_id, "serial": serial}, headers=hdr
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_scan_rule_order_and_auto_start(
    client: AsyncClient, headers: Headers, make_stop: MakeStop, db_session: AsyncSession
) -> None:
    fx = await make_stop()
    theo = await headers(THEO)
    new_tag = fx.new_asset.asset_tag or ""
    first = await _scan(client, theo, fx.sr.id, new_tag.lower())  # normalized
    assert first["verdict"] == "ok"
    assert first["code"] == "new_verified"
    assert first["applied"] is True
    assert first["undo"]["method"] == "DELETE"
    assert first["undo"]["href"] == f"/service-requests/{fx.sr.id}/items/new_base/verification"
    assert first["progress"] == {"done": 1, "total": 2}
    assert "verified" in first["message"]
    await db_session.refresh(fx.tour)
    assert fx.tour.status == TourStatus.IN_PROGRESS  # first stop action auto-starts (#32)

    again = await _scan(client, theo, fx.sr.id, fx.new_asset.serial_number)
    assert again["code"] == "already_verified"
    assert again["applied"] is False

    old = await _scan(client, theo, fx.sr.id, fx.old_asset.serial_number)
    assert old["code"] == "old_verified"
    assert old["progress"] == {"done": 2, "total": 2}

    unknown = await _scan(client, theo, fx.sr.id, "ZZ-NOT-A-DEVICE")
    assert unknown["verdict"] == "warning"
    assert unknown["code"] == "unknown_device"
    assert unknown["canRegister"] is True
    assert unknown["suggestedSlots"] == ["old_peripheral_1"]

    stranger = await f.make_asset(db_session)
    other = await _scan(client, theo, fx.sr.id, stranger.serial_number)
    assert other["verdict"] == "error"
    assert other["code"] == "not_on_this_request"


async def test_scan_device_of_other_request(
    client: AsyncClient, headers: Headers, make_stop: MakeStop
) -> None:
    fx = await make_stop()
    other = await make_stop(tour_date=fx.tour.tour_date + timedelta(days=7))
    result = await _scan(client, await headers(DANA), fx.sr.id, other.new_asset.serial_number)
    assert result["code"] == "belongs_to_other_sr"
    assert other.sr.number in result["message"]


async def test_scan_permissions_and_context(
    client: AsyncClient, headers: Headers, make_stop: MakeStop
) -> None:
    fx = await make_stop()
    body = {"serviceRequestId": fx.sr.id, "serial": "X"}
    tara = await client.post("/api/v1/scan/deployment", json=body, headers=await headers(TARA))
    assert tara.status_code == 403
    assert tara.json()["code"] == "not_own_tour"
    vic = await client.post("/api/v1/scan/deployment", json=body, headers=await headers(VIC))
    assert vic.status_code == 403
    assert vic.json()["code"] == "forbidden"
    anon = await client.post("/api/v1/scan/deployment", json=body)
    assert anon.status_code == 401
    bad = await client.post(
        "/api/v1/scan/deployment", json={"serviceRequestId": fx.sr.id}, headers=await headers(THEO)
    )
    assert bad.status_code == 422
    assert "serial" in bad.json()["errors"]

    not_ready = await make_stop(
        sr_status=SR.READY_FOR_PICKING, tour_date=fx.tour.tour_date + timedelta(days=8)
    )
    response = await client.post(
        "/api/v1/scan/deployment",
        json={"serviceRequestId": not_ready.sr.id, "serial": "X"},
        headers=await headers(THEO),
    )
    assert response.status_code == 409
    assert "not handed over" in response.json()["detail"]


async def test_scan_on_future_tour_is_refused(
    client: AsyncClient, headers: Headers, make_stop: MakeStop
) -> None:
    from app.core.timeutil import business_today

    fx = await make_stop(tour_date=business_today() + timedelta(days=3))
    response = await client.post(
        "/api/v1/scan/deployment",
        json={"serviceRequestId": fx.sr.id, "serial": fx.new_asset.serial_number},
        headers=await headers(THEO),
    )
    assert response.status_code == 409
    assert "not on the road" in response.json()["detail"]


async def test_undo_verification(
    client: AsyncClient, headers: Headers, make_stop: MakeStop
) -> None:
    fx = await make_stop()
    theo = await headers(THEO)
    await _scan(client, theo, fx.sr.id, fx.new_asset.serial_number)
    url = f"/api/v1/service-requests/{fx.sr.id}/items/new_base/verification"
    response = await client.delete(url, headers=theo)
    assert response.status_code == 200
    assert response.json()["verifiedAt"] is None
    again = await client.delete(url, headers=theo)
    assert again.status_code == 409
    assert again.json()["code"] == "undo_not_possible"


async def test_register_unknown_device(
    client: AsyncClient, headers: Headers, make_stop: MakeStop, db_session: AsyncSession
) -> None:
    fx = await make_stop()
    theo = await headers(THEO)
    url = f"/api/v1/service-requests/{fx.sr.id}/items/old_peripheral_1/register-unknown"
    response = await client.post(
        url,
        json={"serial": "kd-found-001", "assetTag": "gx199999", "model": "KD-DOCK-U3"},
        headers=theo,
    )
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["slot"] == "old_peripheral_1"
    assert item["verifiedAt"] is not None
    assert item["asset"]["serialNumber"] == "KD-FOUND-001"
    assert item["asset"]["assetTag"] == "GX199999"
    assert item["asset"]["article"]["modelNumber"] == "KD-DOCK-U3"
    asset = (
        await db_session.scalars(select(Asset).where(Asset.serial_number == "KD-FOUND-001"))
    ).one()
    assert asset.status == AssetStatus.IN_USE
    assert asset.source == AssetSource.TECHNICIAN_SCAN

    duplicate = await client.post(
        url.replace("old_peripheral_1", "old_peripheral_2"),
        json={"serial": "KD-FOUND-001"},
        headers=theo,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "duplicate"
    new_slot = await client.post(
        url.replace("old_peripheral_1", "new_base"), json={"serial": "X1"}, headers=theo
    )
    assert new_slot.status_code == 422
    undo = await client.delete(
        f"/api/v1/service-requests/{fx.sr.id}/items/old_peripheral_1/verification", headers=theo
    )
    assert undo.status_code == 409
    assert undo.json()["code"] == "undo_not_possible"


async def test_deployment_view(client: AsyncClient, headers: Headers, make_stop: MakeStop) -> None:
    fx = await make_stop()
    url = f"/api/v1/tours/{fx.tour.id}/stops/{fx.stop.id}/deployment"
    response = await client.get(url, headers=await headers(THEO))
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["serviceRequest"]["number"] == fx.sr.number
    assert view["stop"]["readiness"] == "ready"
    assert view["progress"] == {"newVerified": 0, "newTotal": 1, "oldVerified": 0, "oldTotal": 1}
    assert len(view["deviationCodes"]) == 16
    assert "complete_deployment" not in view["serviceRequest"]["availableActions"]
    blocked = {b["action"]: b["guard"] for b in view["serviceRequest"]["blockedActions"]}
    assert blocked["complete_deployment"] == "deployment_scans_complete"
    assert "reschedule_on_site" in view["serviceRequest"]["availableActions"]
    assert view["workInstructions"]["deliveryAddress"]["city"] == "Riverton"
    tara = await client.get(url, headers=await headers(TARA))
    assert tara.status_code == 403
    assert tara.json()["code"] == "not_own_tour"
    missing = await client.get(
        f"/api/v1/tours/{fx.tour.id}/stops/999999/deployment", headers=await headers(THEO)
    )
    assert missing.status_code == 404


async def test_own_tour_authorizer_hides_actions(
    client: AsyncClient,
    headers: Headers,
    make_stop: MakeStop,
    db_session: AsyncSession,
    user: Callable[[str], Awaitable[User]],
) -> None:
    fx = await make_stop()

    async def actions_for(email: str) -> list[str]:
        return (await build_sr_detail(db_session, fx.sr, await user(email))).available_actions

    assert "reschedule_on_site" in await actions_for(THEO)
    assert "reschedule_on_site" not in await actions_for(TARA)
    dana = await actions_for(DANA)
    assert "reschedule_on_site" in dana
    assert "complete_manually" in dana
    assert "request_reschedule" in dana
    response = await client.post(
        f"/api/v1/service-requests/{fx.sr.id}/actions/reschedule_on_site",
        json={"deviationCodeId": 4},
        headers=await headers(TARA),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "not_own_tour"


async def test_complete_deployment_happy_path(
    client: AsyncClient,
    headers: Headers,
    make_stop: MakeStop,
    db_session: AsyncSession,
    jobs: InMemoryConnector,
) -> None:
    fx = await make_stop()
    lease = await f.make_lease(
        db_session,
        status=LeaseReturnStatus.REPLACEMENT_ORDERED,
        sr=fx.sr,
    )
    lease.asset_tag = fx.old_asset.asset_tag or ""
    await db_session.flush()
    theo = await headers(THEO)
    url = f"/api/v1/service-requests/{fx.sr.id}/actions/complete_deployment"
    blocked = await client.post(
        url, json={"recipientName": "Mia Turner", "handoverConfirmed": True}, headers=theo
    )
    assert blocked.status_code == 409
    assert blocked.json()["guard"]["code"] == "deployment_scans_complete"

    await _scan(client, theo, fx.sr.id, fx.new_asset.serial_number)
    await _scan(client, theo, fx.sr.id, fx.old_asset.asset_tag or "")
    unconfirmed = await client.post(
        url, json={"recipientName": "Mia Turner", "handoverConfirmed": False}, headers=theo
    )
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["guard"]["code"] == "deployment_scans_complete"
    missing_name = await client.post(url, json={"handoverConfirmed": True}, headers=theo)
    assert missing_name.status_code == 422
    assert "recipientName" in missing_name.json()["errors"]

    response = await client.post(
        url, json={"recipientName": "Mia Turner", "handoverConfirmed": True}, headers=theo
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["status"] == "completed"
    assert detail["protocolAvailable"] is True
    assert detail["milestones"]["completedAt"] is not None
    await db_session.refresh(fx.new_asset)
    await db_session.refresh(fx.old_asset)
    await db_session.refresh(lease)
    await db_session.refresh(fx.stop)
    await db_session.refresh(fx.tour)
    assert fx.new_asset.status == AssetStatus.IN_USE
    assert fx.old_asset.status == AssetStatus.COLLECTED
    assert lease.status == LeaseReturnStatus.CLOSED
    assert lease.close_reason == "device_collected"
    assert fx.stop.outcome == TourStopOutcome.COMPLETED
    assert fx.tour.status == TourStatus.COMPLETED  # last pending stop → auto-complete (#32)
    mails = [a for a in deferred(jobs) if a["template_id"] == "sr.protocol"]
    assert len(mails) == 1
    assert mails[0]["attachment_file_ids"]

    stored = await db_session.get(StoredFile, fx.sr.protocol_file_id)
    assert stored is not None
    assert stored.purpose == "protocol"
    pdf = PdfReader(io.BytesIO(file_store().read(stored.storage_key)))
    assert len(pdf.pages) == 2
    page1, page2 = (p.extract_text() for p in pdf.pages)
    assert "Data deletion consent" in page1
    assert (fx.old_asset.asset_tag or "") in page1
    assert "Hardware receipt" in page2
    assert "Mia Turner" in page2


async def test_complete_with_deviation_and_validation(
    client: AsyncClient, headers: Headers, make_stop: MakeStop, db_session: AsyncSession
) -> None:
    fx = await make_stop()
    theo = await headers(THEO)
    url = f"/api/v1/service-requests/{fx.sr.id}/actions/complete_deployment"
    base = {"recipientName": "Mia Turner", "handoverConfirmed": True, "sendProtocol": False}
    no_comment = await client.post(url, json={**base, "deviationCodeId": 10}, headers=theo)
    assert no_comment.status_code == 422
    assert "deviationComment" in no_comment.json()["errors"]
    wrong = await client.post(url, json={**base, "deviationCodeId": 4}, headers=theo)
    assert wrong.status_code == 409
    assert wrong.json()["guard"]["code"] == "deviation_outcome"
    ok = await client.post(
        url,
        json={**base, "deviationCodeId": 10, "deviationComment": "User keeps the old laptop"},
        headers=theo,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["deviation"]["code"]["code"] == "DV10"
    await db_session.refresh(fx.new_asset)
    await db_session.refresh(fx.old_asset)
    assert fx.new_asset.status == AssetStatus.AVAILABLE  # unverified new device released
    assert fx.old_asset.status == AssetStatus.IN_USE  # not verified → stays at the user


async def test_reschedule_on_site(
    client: AsyncClient, headers: Headers, make_stop: MakeStop, db_session: AsyncSession
) -> None:
    fx = await make_stop()
    theo = await headers(THEO)
    url = f"/api/v1/service-requests/{fx.sr.id}/actions/reschedule_on_site"
    wrong = await client.post(
        url, json={"deviationCodeId": 15, "deviationComment": "x"}, headers=theo
    )
    assert wrong.status_code == 409
    assert wrong.json()["guard"]["code"] == "deviation_outcome"
    missing = await client.post(url, json={}, headers=theo)
    assert missing.status_code == 422
    response = await client.post(url, json={"deviationCodeId": 4}, headers=theo)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["status"] == "reschedule_needed"
    assert detail["resumeStatus"] == "ready_for_picking"
    assert detail["appointmentStartAt"] is None
    await db_session.refresh(fx.new_asset)
    await db_session.refresh(fx.stop)
    await db_session.refresh(fx.appointment)
    assert fx.new_asset.status == AssetStatus.PREPARED  # return_to_hub
    assert fx.stop.outcome == TourStopOutcome.RESCHEDULED
    assert fx.appointment.is_current is False
    assert fx.appointment.superseded_at is not None
    assert fx.appointment.status == "booked"  # provider booking stays (#7)


async def test_cancel_on_site(
    client: AsyncClient, headers: Headers, make_stop: MakeStop, db_session: AsyncSession
) -> None:
    fx = await make_stop()
    theo = await headers(THEO)
    url = f"/api/v1/service-requests/{fx.sr.id}/actions/cancel_on_site"
    no_comment = await client.post(url, json={"deviationCodeId": 15}, headers=theo)
    assert no_comment.status_code == 422
    assert "deviationComment" in no_comment.json()["errors"]
    response = await client.post(
        url,
        json={"deviationCodeId": 16, "deviationComment": "User refused the exchange."},
        headers=theo,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    await db_session.refresh(fx.new_asset)
    await db_session.refresh(fx.stop)
    assert fx.new_asset.status == AssetStatus.AVAILABLE
    assert fx.stop.outcome == TourStopOutcome.CANCELLED


async def test_complete_manually(
    client: AsyncClient, headers: Headers, make_stop: MakeStop, db_session: AsyncSession
) -> None:
    fx = await make_stop()
    url = f"/api/v1/service-requests/{fx.sr.id}/actions/complete_manually"
    theo = await client.post(url, json={"comment": "x"}, headers=await headers(THEO))
    assert theo.status_code == 403
    dana = await headers(DANA)
    no_comment = await client.post(url, json={}, headers=dana)
    assert no_comment.status_code == 422
    response = await client.post(url, json={"comment": "User installed it himself."}, headers=dana)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    await db_session.refresh(fx.new_asset)
    await db_session.refresh(fx.old_asset)
    await db_session.refresh(fx.stop)
    assert fx.new_asset.status == AssetStatus.IN_USE
    assert fx.old_asset.status == AssetStatus.IN_USE  # untouched
    assert fx.stop.outcome == TourStopOutcome.COMPLETED


async def test_protocol_download_and_resend(
    client: AsyncClient, headers: Headers, db_session: AsyncSession, jobs: InMemoryConnector
) -> None:
    from app.core.models import ServiceRequest

    sr = (
        await db_session.scalars(
            select(ServiceRequest).where(ServiceRequest.status == SR.COMPLETED).limit(1)
        )
    ).one()
    link = await client.get(
        f"/api/v1/service-requests/{sr.id}/protocol", headers=await headers(VIC)
    )
    assert link.status_code == 200
    assert link.json()["contentType"] == "application/pdf"
    assert "/files/" in link.json()["url"]
    open_sr = await f.make_sr(db_session, status=SR.INVITED)
    missing = await client.get(
        f"/api/v1/service-requests/{open_sr.id}/protocol", headers=await headers(VIC)
    )
    assert missing.status_code == 404
    send = await client.post(
        f"/api/v1/service-requests/{sr.id}/protocol/send",
        json={"email": "someone.else@example.com"},
        headers=await headers(DANA),
    )
    assert send.status_code == 202
    assert send.json()["notificationId"]
    [mail] = [a for a in deferred(jobs) if a["template_id"] == "sr.protocol"]
    assert mail["to"] == ["someone.else@example.com"]
    viewer = await client.post(
        f"/api/v1/service-requests/{sr.id}/protocol/send", json={}, headers=await headers(VIC)
    )
    assert viewer.status_code == 403
