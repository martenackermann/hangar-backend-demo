"""Assets: list/detail/movements/patch/export and the H5 asset actions (API.md §8.3, #24)."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AssetStatus
from app.core.models import Asset, AssetMovement
from app.testing import factories as f

from .helpers import ADA, DANA, THEO, VIC, Headers, get_json

URL = "/api/v1/assets"


async def test_list_filters_sort_and_pagination(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    auth = await headers(VIC)
    page = await get_json(client, URL, auth, perPage=10)
    assert page["meta"]["perPage"] == 10
    assert page["meta"]["total"] > 500
    changed = [a["statusChangedAt"] for a in page["data"]]
    assert changed == sorted(changed, reverse=True)
    item = page["data"][0]
    assert set(item) >= {"site", "assignedContact", "checkedInAt", "source", "availableActions"}
    assert item["availableActions"] == []  # viewer
    stock = await get_json(client, URL, auth, status="available", perPage=200)
    assert all(a["status"] == "available" for a in stock["data"])
    hil = await f.site(db_session, "HIL")
    at_hil = await get_json(client, URL, auth, siteId=str(hil.id), locationType="staging")
    assert at_hil["data"]
    assert all(
        a["location"]["siteId"] == hil.id and a["location"]["type"] == "staging"
        for a in at_hil["data"]
    )
    monitors = await get_json(client, URL, auth, category="monitor")
    assert all(a["article"]["category"] == "monitor" for a in monitors["data"])
    placed = await get_json(client, URL, auth, hasInstallationPlace="true")
    assert all(a["installationPlace"] for a in placed["data"])
    place = placed["data"][0]["installationPlace"]
    by_place = await get_json(client, URL, auth, installationPlace=place.lower())
    assert all(a["installationPlace"] == place for a in by_place["data"])
    serial = placed["data"][0]["serialNumber"]
    found = await get_json(client, URL, auth, q=serial[2:8].lower())
    assert serial in [a["serialNumber"] for a in found["data"]]
    sorted_page = await get_json(client, URL, auth, sort="serialNumber")
    serials = [a["serialNumber"] for a in sorted_page["data"]]
    assert serials == sorted(serials)
    seeded = await get_json(client, URL, auth, source="seed,manual", perPage=1)
    assert seeded["meta"]["total"] >= 0
    beyond = await get_json(client, URL, auth, page=9999)
    assert beyond["data"] == []
    for params in ({"status": "lost"}, {"sort": "color"}, {"siteId": "x"}, {"perPage": "500"}):
        assert (await client.get(URL, headers=auth, params=params)).status_code == 422
    assert (await client.get(URL)).status_code == 401


async def test_detail_by_serial_and_movements(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    auth = await headers(DANA)
    asset = (
        await db_session.scalars(
            select(Asset).where(Asset.status == AssetStatus.IN_USE, Asset.asset_tag.is_not(None))
        )
    ).first()
    assert asset is not None
    detail = await get_json(client, f"{URL}/{asset.id}", auth)
    assert detail["serialNumber"] == asset.serial_number
    assert detail["recentMovements"]
    assert detail["version"] == asset.version
    assert "blockedActions" in detail
    by_serial = await get_json(client, f"{URL}/by-serial/{asset.serial_number.lower()}", auth)
    assert by_serial["id"] == asset.id
    by_tag = await get_json(client, f"{URL}/by-serial/{asset.asset_tag}", auth)
    assert by_tag["id"] == asset.id
    movements = await get_json(client, f"{URL}/{asset.id}/movements", auth, perPage=2)
    assert movements["meta"]["total"] >= 1
    assert movements["data"][0]["actionLabel"]
    assert (await client.get(f"{URL}/999999", headers=auth)).status_code == 404
    assert (await client.get(f"{URL}/by-serial/NOPE", headers=auth)).status_code == 404


async def test_patch_metadata(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    asset = await f.make_asset(db_session)
    other = await f.make_asset(db_session)
    auth = await headers(DANA)
    response = await client.patch(
        f"{URL}/{asset.id}",
        headers=auth,
        json={"assetTag": " gx999001 ", "macAddress": "02-00-5e-10-20-30", "ciName": "LT-RIV-9"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assetTag"] == "GX999001"
    assert body["macAddress"] == "02:00:5E:10:20:30"
    assert body["ciName"] == "LT-RIV-9"
    clash = await client.patch(f"{URL}/{other.id}", headers=auth, json={"assetTag": "GX999001"})
    assert clash.status_code == 409
    assert clash.json()["code"] == "conflict"
    bad_mac = await client.patch(f"{URL}/{asset.id}", headers=auth, json={"macAddress": "zz"})
    assert bad_mac.status_code == 422
    assert "macAddress" in bad_mac.json()["errors"]
    status_change = await client.patch(
        f"{URL}/{asset.id}", headers=auth, json={"status": "missing"}
    )
    assert status_change.status_code == 422  # status only through actions
    stale = await client.patch(
        f"{URL}/{asset.id}", headers=auth, json={"version": 1, "ciName": "x"}
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "version_conflict"
    viewer = await client.patch(f"{URL}/{asset.id}", headers=await headers(VIC), json={})
    assert viewer.status_code == 403


async def test_mark_missing_and_found(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    asset = await f.make_asset(db_session, location_code="RIV-STAGING", place="C2-07")
    auth = await headers(DANA)
    detail = await get_json(client, f"{URL}/{asset.id}", auth)
    assert set(detail["availableActions"]) == {"mark_missing", "correct"}
    assert "deactivate" not in detail["availableActions"]
    missing = await client.post(
        f"{URL}/{asset.id}/actions/mark_missing", headers=auth, json={"reason": "Not on shelf"}
    )
    assert missing.status_code == 200, missing.text
    assert missing.json()["status"] == "missing"
    assert missing.json()["recentMovements"][0]["reason"] == "Not on shelf"
    assert set(missing.json()["availableActions"]) == {"mark_found", "correct"}
    goods_in = await f.location(db_session, "HIL-GOODS_IN")
    found = await client.post(
        f"{URL}/{asset.id}/actions/mark_found", headers=auth, json={"locationId": goods_in.id}
    )
    assert found.status_code == 200, found.text
    assert found.json()["status"] == "available"
    assert found.json()["location"]["code"] == "HIL-GOODS_IN"
    again = await client.post(
        f"{URL}/{asset.id}/actions/mark_found", headers=auth, json={"locationId": goods_in.id}
    )
    assert again.status_code == 409
    assert again.json()["code"] == "transition_not_allowed"


async def test_mark_found_validation(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    asset = await f.make_asset(db_session, status=AssetStatus.MISSING)
    auth = await headers(DANA)
    in_use = await f.location(db_session, "IN_USE")
    wrong = await client.post(
        f"{URL}/{asset.id}/actions/mark_found", headers=auth, json={"locationId": in_use.id}
    )
    assert wrong.status_code == 422
    staging = await f.location(db_session, "EAS-STAGING")
    bad_place = await client.post(
        f"{URL}/{asset.id}/actions/mark_found",
        headers=auth,
        json={"locationId": staging.id, "installationPlace": "nowhere"},
    )
    assert bad_place.status_code == 422
    assert "installationPlace" in bad_place.json()["errors"]
    missing_field = await client.post(f"{URL}/{asset.id}/actions/mark_found", headers=auth, json={})
    assert missing_field.status_code == 422
    ok = await client.post(
        f"{URL}/{asset.id}/actions/mark_found",
        headers=auth,
        json={"locationId": staging.id, "installationPlace": "loc:d3-11"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["installationPlace"] == "D3-11"


async def test_correct_requires_reason_and_keeps_invariants(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    asset = await f.make_asset(db_session)
    auth = await headers(ADA)
    no_reason = await client.post(
        f"{URL}/{asset.id}/actions/correct", headers=auth, json={"status": "collected"}
    )
    assert no_reason.status_code == 422
    assert "reason" in no_reason.json()["errors"]
    invalid = await client.post(
        f"{URL}/{asset.id}/actions/correct",
        headers=auth,
        json={"status": "collected", "reason": "Wrong record"},
    )
    assert invalid.status_code == 422  # collected ⇒ return location
    ret = await f.location(db_session, "RIV-RETURN")
    ok = await client.post(
        f"{URL}/{asset.id}/actions/correct",
        headers=auth,
        json={"status": "collected", "locationId": ret.id, "reason": "Found in the return cage"},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "collected"
    assert body["installationPlace"] is None
    move = body["recentMovements"][0]
    assert move["action"] == "correct"
    assert move["reason"] == "Found in the return cage"
    assert move["actor"]["email"] == ADA


async def test_actions_permissions_and_deactivate_not_exposed(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    asset = await f.make_asset(db_session)
    for persona in (THEO, VIC):
        response = await client.post(
            f"{URL}/{asset.id}/actions/mark_missing", headers=await headers(persona), json={}
        )
        assert response.status_code == 403
    deactivate = await client.post(
        f"{URL}/{asset.id}/actions/deactivate", headers=await headers(DANA), json={}
    )
    assert deactivate.status_code == 404
    assert deactivate.json()["code"] == "unknown_action"


async def test_bulk_mark_missing_and_correct(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    a = await f.make_asset(db_session)
    b = await f.make_asset(db_session)
    in_use = await f.make_asset(db_session, status=AssetStatus.IN_USE, location_code="IN_USE")
    a_id, b_id, in_use_id = a.id, b.id, in_use.id  # a failed bulk item rolls back + expires
    goods_in = await f.location(db_session, "EAS-GOODS_IN")
    goods_in_id = goods_in.id
    auth = await headers(DANA)
    response = await client.post(
        f"{URL}/bulk/mark_missing",
        headers=auth,
        json={"ids": [a_id, b_id, in_use_id], "reason": "Inventory gap"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["succeeded"] == [a_id, b_id]
    assert result["failed"][0]["id"] == in_use_id
    assert result["failed"][0]["code"] == "transition_not_allowed"
    corrected = await client.post(
        f"{URL}/bulk/correct",
        headers=auth,
        json={
            "ids": [a_id, b_id],
            "status": "available",
            "locationId": goods_in_id,
            "reason": "Recount",
        },
    )
    assert corrected.status_code == 200
    assert corrected.json()["succeeded"] == [a_id, b_id]
    refreshed = (
        await db_session.scalars(
            select(Asset)
            .where(Asset.id.in_([a_id, b_id]))
            .execution_options(populate_existing=True)
        )
    ).all()
    assert {x.location_id for x in refreshed} == {goods_in_id}
    found = await client.post(f"{URL}/bulk/mark_found", headers=auth, json={"ids": [a_id]})
    assert found.status_code == 404  # not a bulk action
    moves = (
        await db_session.scalars(
            select(AssetMovement).where(AssetMovement.asset_id == a_id).order_by(AssetMovement.id)
        )
    ).all()
    assert [m.action for m in moves][-2:] == ["mark_missing", "correct"]


async def test_export(client: AsyncClient, headers: Headers) -> None:
    assert (await client.post(f"{URL}/export", headers=await headers(VIC))).status_code == 403
    response = await client.post(
        f"{URL}/export", headers=await headers(DANA), params={"status": "missing"}
    )
    assert response.status_code == 200, response.text
    link = response.json()
    download = await client.get("/api/v1" + link["url"].split("/api/v1", 1)[1])
    lines = download.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0] == (
        "Serial,Asset tag,Article number,Article,Model,Status,Location,"
        "Installation place,SR number,Checked in"
    )
    assert len(lines) - 1 == 4  # the seeded missing devices
    assert all(",Missing," in line for line in lines[1:])
