"""Installation place scan station: verdict table, undo, occupancy list (SPEC §6.16)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assets import asset_service
from app.core.enums import AssetAction, AssetStatus, LocationType, ServiceRequestStatus, Trigger
from app.core.models import Asset, AssetMovement
from app.modules.h3_staging.tests.helpers import API, DANA, THEO, VIC
from app.testing import factories as f

type Headers = Callable[[str], Awaitable[dict[str, str]]]
type Stock = Callable[..., Awaitable[Asset]]


async def _scan(
    client: AsyncClient, headers: Headers, serial: str, place: str, site_id: int, who: str = THEO
) -> Any:
    response = await client.post(
        f"{API}/scan/installation-place",
        json={"siteId": site_id, "serial": serial, "place": place},
        headers=await headers(who),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_place_and_verdicts(
    client: AsyncClient, headers: Headers, stock: Stock, db_session: AsyncSession
) -> None:
    riv = await f.site(db_session, "RIV")
    asset = await stock(location_code="RIV-GOODS_IN", place=None)

    body = await _scan(client, headers, asset.serial_number, "bogus", riv.id)
    assert (body["verdict"], body["code"], body["applied"]) == ("error", "invalid_place", False)
    body = await _scan(client, headers, "UNKNOWN-1", "B4-12", riv.id)
    assert (body["verdict"], body["code"]) == ("error", "unknown_serial")

    body = await _scan(client, headers, asset.serial_number.lower(), "loc:y7-12", riv.id)
    assert (body["verdict"], body["code"], body["applied"]) == ("ok", "placed", True)
    assert body["entity"]["installationPlace"] == "Y7-12"
    assert body["entity"]["location"]["code"] == "RIV-STAGING"
    assert body["scannedValue"] == asset.serial_number
    movement_id = body["undo"]["body"]["movementId"]
    assert body["undo"] == {
        "method": "POST",
        "href": "/scan/installation-place/undo",
        "body": {"movementId": movement_id},
    }
    movement = await db_session.get(AssetMovement, movement_id)
    assert movement is not None
    assert movement.action == AssetAction.PLACE
    assert movement.source_ref == "scan:installation_place"

    body = await _scan(client, headers, asset.serial_number, "Y7-12", riv.id)
    assert (body["verdict"], body["code"], body["applied"]) == ("ok", "unchanged", False)

    neighbour = await stock(location_code="RIV-GOODS_IN", place=None)
    body = await _scan(client, headers, neighbour.asset_tag or "", "Y7-12", riv.id)
    assert (body["verdict"], body["code"], body["applied"]) == ("warning", "place_occupied", True)

    hil = await f.site(db_session, "HIL")
    body = await _scan(client, headers, neighbour.serial_number, "Y7-13", hil.id)
    assert (body["verdict"], body["code"]) == ("warning", "moved_from_other_site")
    assert body["entity"]["location"]["code"] == "HIL-STAGING"


async def test_not_available_not_in_stock_unknown_model(
    client: AsyncClient, headers: Headers, stock: Stock, db_session: AsyncSession
) -> None:
    riv = await f.site(db_session, "RIV")
    sr = await f.make_sr(db_session)
    assigned = await f.make_asset(db_session, status=AssetStatus.ASSIGNED, sr=sr)
    body = await _scan(client, headers, assigned.serial_number, "Y8-01", riv.id)
    assert (body["verdict"], body["code"]) == ("error", "not_available")
    assert sr.number in body["message"]

    at_goods_out = await stock(location_code="RIV-GOODS_OUT", place=None)
    body = await _scan(client, headers, at_goods_out.serial_number, "Y8-01", riv.id)
    assert (body["verdict"], body["code"]) == ("error", "not_in_stock")

    no_model = await stock(location_code="RIV-GOODS_IN", place=None)
    no_model.article = None
    no_model.article_id = None
    no_model.model = "XR-UNKNOWN"
    await db_session.flush()
    body = await _scan(client, headers, no_model.serial_number, "Y8-02", riv.id)
    assert (body["verdict"], body["code"], body["applied"]) == ("warning", "unknown_model", True)


async def test_scan_http_errors(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    riv = await f.site(db_session, "RIV")
    campus = await f.site(db_session, "CMP")
    url = f"{API}/scan/installation-place"
    body = {"siteId": riv.id, "serial": "X", "place": "A1-01"}
    assert (await client.post(url, json=body, headers=await headers(VIC))).status_code == 403
    assert (await client.post(url, json=body)).status_code == 401
    missing = await client.post(
        url, json={"siteId": riv.id, "serial": "X"}, headers=await headers(THEO)
    )
    assert missing.status_code == 422
    assert "place" in missing.json()["errors"]
    not_hub = await client.post(
        url, json={**body, "siteId": campus.id}, headers=await headers(THEO)
    )
    assert not_hub.status_code == 422
    unknown = await client.post(url, json={**body, "siteId": 9999}, headers=await headers(THEO))
    assert unknown.status_code == 404


async def test_undo(
    client: AsyncClient, headers: Headers, stock: Stock, db_session: AsyncSession
) -> None:
    riv = await f.site(db_session, "RIV")
    asset = await stock(location_code="RIV-GOODS_IN", place=None)
    first = await _scan(client, headers, asset.serial_number, "Y9-01", riv.id)
    second = await _scan(client, headers, asset.serial_number, "Y9-02", riv.id)
    url = f"{API}/scan/installation-place/undo"

    stale = await client.post(url, json=first["undo"]["body"], headers=await headers(THEO))
    assert stale.status_code == 409
    assert stale.json()["code"] == "undo_not_possible"

    back_to_place = await client.post(url, json=second["undo"]["body"], headers=await headers(THEO))
    assert back_to_place.status_code == 200
    body = back_to_place.json()
    assert (body["verdict"], body["code"]) == ("ok", "undone")
    assert body["entity"]["installationPlace"] == "Y9-01"

    # undoing the first scan is possible again only once it is the latest place movement:
    # the revert above is a new movement, so the original first scan stays stale.
    again = await client.post(url, json=first["undo"]["body"], headers=await headers(THEO))
    assert again.status_code == 409

    fresh = await stock(location_code="RIV-GOODS_IN", place=None)
    placed = await _scan(client, headers, fresh.serial_number, "Y9-03", riv.id)
    reverted = await client.post(url, json=placed["undo"]["body"], headers=await headers(DANA))
    assert reverted.status_code == 200
    await db_session.refresh(fresh)
    assert fresh.location.type == LocationType.GOODS_IN
    assert fresh.installation_place is None
    latest = (
        await db_session.scalars(
            select(AssetMovement)
            .where(AssetMovement.asset_id == fresh.id)
            .order_by(AssetMovement.id.desc())
            .limit(1)
        )
    ).one()
    assert latest.action == AssetAction.CORRECT
    assert latest.reason is not None
    assert "Undo" in latest.reason

    assert (
        await client.post(url, json={"movementId": 10**9}, headers=await headers(THEO))
    ).status_code == 404
    assert (
        await client.post(url, json={"movementId": 1}, headers=await headers(VIC))
    ).status_code == 403


async def test_undo_rejects_other_movements(
    client: AsyncClient, headers: Headers, stock: Stock, db_session: AsyncSession
) -> None:
    sr = await f.make_sr(db_session, status=ServiceRequestStatus.PERSONALIZATION_IN_PROGRESS)
    asset = await stock(place="Y9-09")
    movement = await asset_service.move(
        db_session,
        asset,
        AssetAction.ASSIGN,
        actor=await f.user(db_session, DANA),
        trigger=Trigger.USER,
        service_request=sr,
    )
    response = await client.post(
        f"{API}/scan/installation-place/undo",
        json={"movementId": movement.id},
        headers=await headers(THEO),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "undo_not_possible"


async def test_installation_places_list(
    client: AsyncClient, headers: Headers, stock: Stock, db_session: AsyncSession
) -> None:
    riv = await f.site(db_session, "RIV")
    a = await stock(place="Y10-2")
    b = await stock(place="Y10-10")
    c = await stock(place="Y10-2")
    response = await client.get(
        f"{API}/staging/installation-places",
        params={"siteId": riv.id, "q": "y10"},
        headers=await headers(VIC),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert [row["place"] for row in data] == ["Y10-2", "Y10-10"]
    assert {x["id"] for x in data[0]["assets"]} == {a.id, c.id}
    assert [x["id"] for x in data[1]["assets"]] == [b.id]
    everything = await client.get(
        f"{API}/staging/installation-places", params={"siteId": riv.id}, headers=await headers(VIC)
    )
    assert len(everything.json()["data"]) > 10
    unknown = await client.get(
        f"{API}/staging/installation-places", params={"siteId": 9999}, headers=await headers(VIC)
    )
    assert unknown.status_code == 404
