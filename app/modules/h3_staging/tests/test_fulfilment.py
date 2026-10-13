"""Order closing queue, technician picking, parcel packing and shipments
(SPEC §4.1.1 #23–#25, §6.17; API.md §6.3–6.6; SPEC_RESOLUTIONS #15)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gateways
from app.core.enums import AssetStatus, LocationType
from app.core.gateways.fakes.carrier import FakeParcelCarrier
from app.core.gateways.protocols import CarrierError, ShipmentRequest, ShipmentResult
from app.core.models import ServiceRequest, StatusTransition
from app.core.notifications import render
from app.core.timeutil import business_datetime, business_today, now_utc
from app.modules.h3_staging.models import Shipment
from app.modules.h3_staging.tests.helpers import (
    API,
    DANA,
    SR,
    THEO,
    VIC,
    prepared_sr,
    soon,
)
from app.testing import factories as f

type Headers = Callable[[str], Awaitable[dict[str, str]]]


class RecordingCarrier:
    """Fake carrier that records requests (and optionally fails)."""

    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.requests: list[ShipmentRequest] = []
        self.inner = FakeParcelCarrier()

    async def create_shipment(self, request: ShipmentRequest) -> ShipmentResult:
        self.requests.append(request)
        if self.fail:
            raise CarrierError(self.fail)
        return await self.inner.create_shipment(request)

    async def get_label(self, carrier_shipment_id: str) -> bytes:  # pragma: no cover
        return await self.inner.get_label(carrier_shipment_id)


@pytest.fixture
def carrier() -> Iterator[RecordingCarrier]:
    fake = RecordingCarrier()
    restore = gateways.override(carrier=fake)
    try:
        yield fake
    finally:
        restore()


async def _action(
    client: AsyncClient,
    headers: Headers,
    sr: ServiceRequest,
    action: str,
    who: str = DANA,
    **body: Any,
) -> Any:
    return await client.post(
        f"{API}/service-requests/{sr.id}/actions/{action}", json=body, headers=await headers(who)
    )


# --- order closing queue -----------------------------------------------------------------------


async def test_order_closing_queue(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    hil = await f.site(db_session, "HIL")
    done = await f.make_sr(
        db_session,
        status=SR.PERSONALIZATION_DONE,
        region_code="hillford",
        appointment=soon(1),
        assign_new=True,
        requested_software=["Office Suite"],
        installed_software=["office suite"],
    )
    open_ = await f.make_sr(
        db_session,
        status=SR.PERSONALIZATION_DONE,
        region_code="hillford",
        appointment=soon(2),
        assign_new=True,
        requested_software=["VPN Client"],
    )
    params = {"siteId": hil.id, "perPage": 200}
    body = (
        await client.get(f"{API}/order-closing/queue", params=params, headers=await headers(VIC))
    ).json()
    rows = {r["id"]: r for r in body["data"]}
    assert {done.id, open_.id} <= rows.keys()
    assert all(r["status"] == "personalization_done" for r in body["data"])
    assert rows[done.id]["checklist"]["complete"] is True
    assert rows[open_.id]["checklist"]["missing"] == ["VPN Client"]
    complete = (
        await client.get(
            f"{API}/order-closing/queue",
            params={**params, "checklistComplete": "true"},
            headers=await headers(DANA),
        )
    ).json()
    assert done.id in {r["id"] for r in complete["data"]}
    assert open_.id not in {r["id"] for r in complete["data"]}
    incomplete = (
        await client.get(
            f"{API}/order-closing/queue",
            params={**params, "checklistComplete": "false"},
            headers=await headers(DANA),
        )
    ).json()
    assert open_.id in {r["id"] for r in incomplete["data"]}
    assert done.id not in {r["id"] for r in incomplete["data"]}
    region = await f.region(db_session, "hillford")
    other_region = await f.region(db_session, "riverton")
    by_region = (
        await client.get(
            f"{API}/order-closing/queue",
            params={**params, "regionId": f"{other_region.id}"},
            headers=await headers(DANA),
        )
    ).json()
    assert by_region["data"] == []
    by_region = (
        await client.get(
            f"{API}/order-closing/queue",
            params={**params, "regionId": f"{region.id},none"},
            headers=await headers(DANA),
        )
    ).json()
    assert done.id in {r["id"] for r in by_region["data"]}
    assert (
        await client.get(f"{API}/order-closing/queue", headers=await headers(DANA))
    ).status_code == 422


# --- technician picking --------------------------------------------------------------------------


async def test_technician_picking_and_mark_provided(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    riv = await f.site(db_session, "RIV")
    day = business_today() + timedelta(days=30)
    when = business_datetime(day, 8)
    first = await prepared_sr(
        db_session,
        appointment=when,
        appointment_calendar_id=7,
        appointment_calendar_name="Riverton route",
    )
    second = await prepared_sr(
        db_session,
        appointment=when + timedelta(hours=2),
        appointment_calendar_id=7,
        appointment_calendar_name="Riverton route",
    )
    parcel = await prepared_sr(db_session, region_code="westbay", appointment=when)
    first_id, second_id, parcel_id, riv_id = first.id, second.id, parcel.id, riv.id
    campus = await f.site(db_session, "CMP")
    campus_id = campus.id
    params = {"siteId": riv_id, "date": day.isoformat()}
    body = (
        await client.get(f"{API}/picking/technician", params=params, headers=await headers(VIC))
    ).json()
    assert body["date"] == day.isoformat()
    assert body["site"]["code"] == "RIV"
    [group] = [g for g in body["groups"] if g["calendar"]["id"] == 7]
    assert [r["id"] for r in group["serviceRequests"]][:2] == [first.id, second.id]
    assert body["totals"]["toPick"] >= 2
    assert parcel.id not in {r["id"] for g in body["groups"] for r in g["serviceRequests"]}

    assert (await _action(client, headers, first, "mark_provided", THEO)).status_code == 403
    blocked = await _action(client, headers, parcel, "mark_provided")
    assert blocked.status_code == 409
    assert blocked.json()["guard"]["code"] == "delivery_type_is_technician"
    response = await _action(client, headers, first, "mark_provided")
    assert response.status_code == 200
    detail = response.json()
    assert detail["status"] == "provided"
    assert detail["milestones"]["providedAt"] is not None
    asset = detail["items"][0]["asset"]
    assert (asset["status"], asset["location"]["type"]) == ("provided", "in_transit")

    bulk = await client.post(
        f"{API}/service-requests/bulk/mark_provided",
        json={"ids": [second_id, parcel_id]},
        headers=await headers(DANA),
    )
    assert bulk.status_code == 200
    assert bulk.json()["succeeded"] == [second_id]
    assert bulk.json()["failed"][0]["id"] == parcel_id

    after = (
        await client.get(f"{API}/picking/technician", params=params, headers=await headers(VIC))
    ).json()
    rows = {r["id"]: r for g in after["groups"] for r in g["serviceRequests"]}
    assert rows[first_id]["picked"] is True
    assert rows[second_id]["picked"] is True
    assert after["totals"]["picked"] >= 2

    today = (
        await client.get(
            f"{API}/picking/technician", params={"siteId": riv_id}, headers=await headers(VIC)
        )
    ).json()
    assert today["date"] == business_today().isoformat()
    assert (
        await client.get(
            f"{API}/picking/technician", params={"siteId": campus_id}, headers=await headers(VIC)
        )
    ).status_code == 422


# --- parcel packing ---------------------------------------------------------------------------


async def test_parcel_list_and_packing(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    eas = await f.site(db_session, "EAS")
    sr = await prepared_sr(
        db_session,
        region_code="northport",
        new_types=("Laptop Standard 14", "Monitor 24"),
        old_tags=("GX100777",),
        appointment=now_utc() - timedelta(days=400),  # sorts first
    )
    body = (
        await client.get(
            f"{API}/picking/parcel", params={"siteId": eas.id}, headers=await headers(VIC)
        )
    ).json()
    row = body["data"][0]
    assert row["id"] == sr.id
    assert row["allPacked"] is False
    assert row["hasOldItems"] is True
    ship_to = row["shipTo"]
    assert ship_to["kind"] == "pickup_point"
    assert ship_to["recipientName"] == "Northport Office"
    assert ship_to["name2"].startswith("Attn: ")
    assert ship_to["address"]["zip"] == "40100"
    filtered = (
        await client.get(
            f"{API}/picking/parcel",
            params={"siteId": eas.id, "deliveryType": "parcel_to_user"},
            headers=await headers(VIC),
        )
    ).json()
    assert sr.id not in {r["id"] for r in filtered["data"]}

    laptop, monitor = (i.asset for i in sr.new_items)
    assert laptop is not None
    assert monitor is not None
    url = f"{API}/scan/parcel"

    async def scan(serial: str, who: str = DANA) -> Any:
        return await client.post(
            url, json={"serviceRequestId": sr.id, "serial": serial}, headers=await headers(who)
        )

    assert (await scan(laptop.serial_number, VIC)).status_code == 403
    assert (await scan(laptop.serial_number, THEO)).status_code == 403
    result = (await scan("NOPE")).json()
    assert (result["verdict"], result["code"]) == ("error", "unknown_serial")
    stranger = await f.make_asset(db_session)
    result = (await scan(stranger.serial_number)).json()
    assert (result["verdict"], result["code"]) == ("error", "not_on_this_request")
    result = (await scan(laptop.asset_tag or "")).json()
    assert (result["verdict"], result["code"], result["applied"]) == ("ok", "packed", True)
    assert result["progress"] == {"done": 1, "total": 2}
    assert result["entity"]["packedBy"]["email"] == DANA
    assert result["undo"]["href"] == f"/service-requests/{sr.id}/items/new_base/packing"
    result = (await scan(laptop.serial_number)).json()
    assert (result["verdict"], result["code"], result["applied"]) == ("ok", "already_packed", False)

    unpacked = await client.delete(
        f"{API}/service-requests/{sr.id}/items/new_base/packing", headers=await headers(DANA)
    )
    assert unpacked.status_code == 200
    assert unpacked.json()["packedAt"] is None
    old_slot = await client.delete(
        f"{API}/service-requests/{sr.id}/items/old_base/packing", headers=await headers(DANA)
    )
    assert old_slot.status_code == 422

    technician = await prepared_sr(db_session)
    wrong = await client.post(
        url, json={"serviceRequestId": technician.id, "serial": "X"}, headers=await headers(DANA)
    )
    assert wrong.status_code == 409
    not_ready = await f.make_sr(db_session, status=SR.SHIPPED, region_code="westbay")
    assert (
        await client.post(
            url, json={"serviceRequestId": not_ready.id, "serial": "X"}, headers=await headers(DANA)
        )
    ).status_code == 409
    assert (
        await client.post(
            url, json={"serviceRequestId": 999999, "serial": "X"}, headers=await headers(DANA)
        )
    ).status_code == 404


# --- shipments ------------------------------------------------------------------------------------


async def test_create_shipment(
    client: AsyncClient,
    headers: Headers,
    db_session: AsyncSession,
    carrier: RecordingCarrier,
    jobs: InMemoryConnector,
) -> None:
    sr = await prepared_sr(
        db_session,
        region_code="westbay",
        new_types=("Laptop Standard 14", "Docking Station USB-C"),
        old_tags=("GX100778",),
        packed=True,
        delivery_street="5 Harbour Road",
        delivery_zip="50123",
        delivery_city="Westbay",
        delivery_room="2.14",
    )
    url = f"{API}/service-requests/{sr.id}/shipments"
    assert (await client.post(url, json={}, headers=await headers(VIC))).status_code == 403
    generic = await _action(client, headers, sr, "ship")
    assert generic.status_code == 404
    assert generic.json()["code"] == "unknown_action"

    response = await client.post(
        url,
        json={"includeReturnLabel": True, "combinedParcel": False, "sendConfirmation": True},
        headers=await headers(DANA),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    shipment = body["shipment"]
    assert shipment["carrierShipmentId"].startswith("PC")
    assert shipment["parcelCount"] == 2
    assert len(shipment["trackingUrls"]) == 2
    assert len(shipment["returnTrackingUrls"]) == 1
    assert shipment["includeReturnLabel"] is True
    assert shipment["confirmationMailSent"] is True
    assert shipment["deliveryAddress"]["street"] == "5 Harbour Road"
    assert shipment["createdBy"]["email"] == DANA
    assert body["warnings"] == []
    assert body["label"]["contentType"] == "application/pdf"
    assert "/files/" in body["label"]["url"]
    detail = body["serviceRequest"]
    assert detail["status"] == "shipped"
    assert detail["milestones"]["shippedAt"] is not None
    assert {i["asset"]["status"] for i in detail["items"] if i["direction"] == "deliver"} == {
        "provided"
    }
    assert "ship" not in detail["availableActions"]

    [request] = carrier.requests
    assert request.reference == sr.number
    assert request.recipient.name2 == "Room 2.14"
    assert request.return_address is not None
    assert [p.references[1] for p in request.parcels] == [sr.number, sr.number]

    transition = (
        await db_session.scalars(
            select(StatusTransition).where(
                StatusTransition.entity_id == sr.id, StatusTransition.action == "ship"
            )
        )
    ).one()
    assert transition.payload is not None
    assert transition.payload["carrierShipmentId"] == shipment["carrierShipmentId"]

    [job] = [j for j in jobs.jobs.values() if j["task_name"] == "notifications.send"]
    assert job["args"]["template_id"] == "sr.shipping_confirmation"
    assert job["args"]["to"] == [sr.contact.email]
    mail = render("sr.shipping_confirmation", job["args"]["context"])
    assert sr.number in mail.subject
    assert shipment["trackingUrls"][0] in mail.html
    assert "return label" in mail.text

    listed = await client.get(
        f"{API}/service-requests/{sr.id}/shipments", headers=await headers(VIC)
    )
    assert [s["id"] for s in listed.json()["data"]] == [shipment["id"]]
    label = await client.get(f"{API}/shipments/{shipment['id']}/label", headers=await headers(DANA))
    assert label.status_code == 200
    download = await client.get(API + label.json()["url"].split("/api/v1", 1)[1])
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")
    assert (
        await client.get(f"{API}/shipments/{shipment['id']}/label", headers=await headers(VIC))
    ).status_code == 403
    assert (
        await client.get(f"{API}/shipments/999999/label", headers=await headers(DANA))
    ).status_code == 404

    again = await client.post(url, json={}, headers=await headers(DANA))
    assert again.status_code == 409
    assert again.json()["code"] == "transition_not_allowed"


async def test_shipment_options_and_guards(
    client: AsyncClient,
    headers: Headers,
    db_session: AsyncSession,
    carrier: RecordingCarrier,
    jobs: InMemoryConnector,
) -> None:
    unpacked = await prepared_sr(db_session, region_code="lakeside")
    blocked = await client.post(
        f"{API}/service-requests/{unpacked.id}/shipments", json={}, headers=await headers(DANA)
    )
    assert blocked.status_code == 409
    assert blocked.json()["guard"]["code"] == "all_new_items_packed"
    technician = await prepared_sr(db_session, packed=True)
    blocked = await client.post(
        f"{API}/service-requests/{technician.id}/shipments", json={}, headers=await headers(DANA)
    )
    assert blocked.json()["guard"]["code"] == "delivery_type_is_parcel"
    assert carrier.requests == []

    sr = await prepared_sr(
        db_session,
        region_code="highmoor",
        new_types=("Laptop Standard 14", "Monitor 24"),
        packed=True,
    )
    response = await client.post(
        f"{API}/service-requests/{sr.id}/shipments",
        json={"combinedParcel": True, "sendConfirmation": False},
        headers=await headers(DANA),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["warnings"] == ["no_return_items"]
    assert body["shipment"]["parcelCount"] == 1
    assert body["shipment"]["combinedParcel"] is True
    assert body["shipment"]["includeReturnLabel"] is False
    assert body["shipment"]["returnTrackingUrls"] == []
    assert body["shipment"]["confirmationMailSent"] is False
    assert body["shipment"]["recipientName"] == "Highmoor Office"
    [request] = carrier.requests
    assert request.recipient.name2 is not None
    assert request.recipient.name2.startswith("Attn:")
    assert request.parcels[0].references[0] == sr.number
    assert float(request.parcels[0].weight_kg) == pytest.approx(1.6 + 4.5)
    assert [j for j in jobs.jobs.values() if j["task_name"] == "notifications.send"] == []

    stale = await prepared_sr(db_session, region_code="oakridge", packed=True)
    conflict = await client.post(
        f"{API}/service-requests/{stale.id}/shipments",
        json={"version": stale.version + 5},
        headers=await headers(DANA),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "version_conflict"
    assert (
        await client.post(
            f"{API}/service-requests/{stale.id}/shipments",
            json={"bogus": True},
            headers=await headers(DANA),
        )
    ).status_code == 422


async def test_carrier_error_changes_nothing(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    failing = RecordingCarrier(fail="Recipient address rejected by carrier")
    restore = gateways.override(carrier=failing)
    try:
        sr = await prepared_sr(db_session, region_code="oakridge", packed=True)
        before = await db_session.scalar(select(func.count()).select_from(Shipment))
        response = await client.post(
            f"{API}/service-requests/{sr.id}/shipments", json={}, headers=await headers(DANA)
        )
    finally:
        restore()
    assert response.status_code == 502
    assert response.json()["code"] == "carrier_error"
    assert response.json()["detail"] == "Recipient address rejected by carrier"
    await db_session.refresh(sr)
    assert sr.status == SR.READY_FOR_PICKING
    assert sr.shipped_at is None
    assert await db_session.scalar(select(func.count()).select_from(Shipment)) == before
    for item in sr.new_items:
        assert item.asset is not None
        await db_session.refresh(item.asset)
        assert item.asset.status == AssetStatus.PREPARED


async def test_fake_carrier_rejects_zip_00000(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    sr = await prepared_sr(
        db_session,
        region_code="lakeside",
        packed=True,
        delivery_street="1 Nowhere Lane",
        delivery_zip="00000",
        delivery_city="Lakeside",
    )
    response = await client.post(
        f"{API}/service-requests/{sr.id}/shipments", json={}, headers=await headers(DANA)
    )
    assert response.status_code == 502
    assert response.json()["code"] == "carrier_error"


async def test_close_without_label(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    sr = await prepared_sr(db_session, region_code="westbay")
    missing = await _action(client, headers, sr, "close_without_label")
    assert missing.status_code == 422
    assert "comment" in missing.json()["errors"]
    technician = await prepared_sr(db_session)
    blocked = await _action(
        client, headers, technician, "close_without_label", comment="Handed over at the desk"
    )
    assert blocked.status_code == 409
    assert blocked.json()["guard"]["code"] == "delivery_type_is_parcel"
    response = await _action(client, headers, sr, "close_without_label", comment="Courier run")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "provided"
    assert body["milestones"]["providedAt"] is not None
    asset = body["items"][0]["asset"]
    assert (asset["status"], asset["location"]["type"]) == (
        AssetStatus.PROVIDED.value,
        LocationType.IN_TRANSIT.value,
    )


async def test_shipments_list(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    eas = await f.site(db_session, "EAS")
    riv = await f.site(db_session, "RIV")
    body = (
        await client.get(f"{API}/shipments", params={"perPage": 5}, headers=await headers(VIC))
    ).json()
    assert body["meta"]["total"] >= 15  # seeded shipped SRs
    created = [row["createdAt"] for row in body["data"]]
    assert created == sorted(created, reverse=True)
    by_site = (
        await client.get(f"{API}/shipments", params={"siteId": riv.id}, headers=await headers(VIC))
    ).json()
    assert by_site["meta"]["total"] == 0  # Riverton is a technician region
    eas_rows = (
        await client.get(
            f"{API}/shipments",
            params={"siteId": eas.id, "perPage": 1},
            headers=await headers(VIC),
        )
    ).json()
    number = eas_rows["data"][0]["serviceRequest"]["number"]
    found = (
        await client.get(f"{API}/shipments", params={"q": number}, headers=await headers(VIC))
    ).json()
    assert {row["serviceRequest"]["number"] for row in found["data"]} == {number}
    week = (
        await client.get(
            f"{API}/shipments",
            params={"from": (business_today() - timedelta(days=7)).isoformat()},
            headers=await headers(VIC),
        )
    ).json()
    assert week["meta"]["total"] <= body["meta"]["total"]
    assert (await client.get(f"{API}/shipments")).status_code == 401


async def test_search_by_shipment_id(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    shipment = (await db_session.scalars(select(Shipment).order_by(Shipment.id).limit(1))).one()
    response = await client.get(
        f"{API}/search", params={"q": shipment.carrier_shipment_id}, headers=await headers(VIC)
    )
    assert response.status_code == 200
    hits = [h for g in response.json()["groups"] for h in g["hits"]]
    assert any(h["id"] == shipment.service_request_id for h in hits)
