"""Personalization: claim / release / finish / skip / reopen, FIFO candidates, assignment by
scan (SPEC §4.1.1 #17–#22, §6.6, SPEC_RESOLUTIONS #35), queue, staging options, checklist."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AssetAction, AssetStatus, LocationType, OsReinstallTiming
from app.core.models import Asset, AssetMovement, ServiceRequest
from app.modules.h3_staging.tests.helpers import ADA, API, DANA, SR, THEO, VIC, assign_all, soon
from app.testing import factories as f

type Headers = Callable[[str], Awaitable[dict[str, str]]]
type MakeSr = Callable[..., Awaitable[ServiceRequest]]
type Stock = Callable[..., Awaitable[Asset]]


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


# --- claim / release ---------------------------------------------------------------------------


async def test_claim_sets_editor(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr
) -> None:
    sr = await make_staging_sr(SR.APPOINTMENT_BOOKED, editor=None)
    response = await _action(client, headers, sr, "claim")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "personalization_in_progress"
    assert body["editor"]["email"] == DANA
    assert body["claimedAt"] is not None
    assert "release" in body["availableActions"]


async def test_claim_permissions_and_guards(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr
) -> None:
    sr = await make_staging_sr(SR.APPOINTMENT_BOOKED, editor=None)
    for who in (VIC, THEO):
        assert (await _action(client, headers, sr, "claim", who)).status_code == 403
    no_appt = await make_staging_sr(SR.APPOINTMENT_BOOKED, editor=None, appointment=None)
    response = await _action(client, headers, no_appt, "claim")
    assert response.status_code == 409
    assert response.json()["guard"]["code"] == "has_appointment"
    # claim is not bulk (SPEC_RESOLUTIONS #17)
    bulk = await client.post(
        f"{API}/service-requests/bulk/claim", json={"ids": [sr.id]}, headers=await headers(DANA)
    )
    assert bulk.status_code == 404


async def test_claim_take_over_and_own_claim(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr
) -> None:
    sr = await make_staging_sr()  # Dana edits
    own = await _action(client, headers, sr, "claim")
    assert own.status_code == 409
    assert own.json()["code"] == "transition_not_allowed"
    take_over = await _action(client, headers, sr, "claim", ADA)
    assert take_over.status_code == 200
    assert take_over.json()["editor"]["email"] == ADA


async def test_release_keeps_assignments(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr, db_session: AsyncSession
) -> None:
    sr = await make_staging_sr()
    await assign_all(db_session, sr)
    response = await _action(client, headers, sr, "release")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "appointment_booked"
    assert body["editor"] is None
    assert all(i["asset"] is not None for i in body["items"] if i["direction"] == "deliver")


async def test_release_requires_editor_or_admin(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr, db_session: AsyncSession
) -> None:
    ada = await f.user(db_session, ADA)
    sr = await make_staging_sr(editor=ada)
    blocked = await _action(client, headers, sr, "release", DANA)
    assert blocked.status_code == 409
    assert blocked.json()["guard"]["code"] == "is_editor_or_admin"
    sr2 = await make_staging_sr()  # Dana edits, admin may release
    assert (await _action(client, headers, sr2, "release", ADA)).status_code == 200


# --- finish / skip / reopen / close -------------------------------------------------------------


async def test_finish_requires_all_assigned_and_editor(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr, db_session: AsyncSession
) -> None:
    sr = await make_staging_sr()
    response = await _action(client, headers, sr, "finish_personalization")
    assert response.status_code == 409
    assert response.json()["guard"]["code"] == "all_new_items_assigned"
    await assign_all(db_session, sr)
    response = await _action(client, headers, sr, "finish_personalization", ADA)
    assert response.json()["guard"]["code"] == "is_editor"
    response = await _action(client, headers, sr, "finish_personalization")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "personalization_done"
    assert body["editor"] is None
    assert body["milestones"]["personalizedAt"] is not None


async def test_skip_preparation_prepares_assets(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr, db_session: AsyncSession
) -> None:
    sr = await make_staging_sr()
    await assign_all(db_session, sr)
    response = await _action(client, headers, sr, "skip_preparation")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready_for_picking"
    assert body["withoutAssetPrep"] is True
    assert body["editor"] is None
    assert body["milestones"]["orderClosedAt"] is not None
    for item in body["items"]:
        if item["direction"] == "deliver":
            assert item["asset"]["status"] == "prepared"
            assert item["asset"]["location"]["type"] == "goods_out"
            assert item["asset"]["installationPlace"] is None


async def test_skip_from_booked_only_without_new_items(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr
) -> None:
    with_items = await make_staging_sr(SR.APPOINTMENT_BOOKED, editor=None)
    response = await _action(client, headers, with_items, "skip_preparation")
    assert response.status_code == 409
    assert response.json()["code"] == "transition_not_allowed"
    removal = await make_staging_sr(
        SR.APPOINTMENT_BOOKED, editor=None, new_types=(), old_tags=("GX199999",)
    )
    response = await _action(client, headers, removal, "skip_preparation")
    assert response.status_code == 200
    assert response.json()["status"] == "ready_for_picking"


async def test_reopen_makes_actor_editor(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr
) -> None:
    sr = await make_staging_sr(SR.PERSONALIZATION_DONE, editor=None, assign_new=True)
    response = await _action(client, headers, sr, "reopen_personalization", ADA)
    assert response.status_code == 200
    assert response.json()["status"] == "personalization_in_progress"
    assert response.json()["editor"]["email"] == ADA


async def test_close_order_needs_checklist(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr
) -> None:
    sr = await make_staging_sr(
        SR.PERSONALIZATION_DONE,
        editor=None,
        assign_new=True,
        requested_software=["PDF Reader", "VPN Client"],
    )
    blocked = await _action(client, headers, sr, "close_order")
    assert blocked.status_code == 409
    assert blocked.json()["guard"]["code"] == "software_checklist_complete"
    saved = await client.put(
        f"{API}/service-requests/{sr.id}/software-checklist",
        json={"installedSoftware": ["pdf reader", "VPN Client "], "addedSoftware": []},
        headers=await headers(DANA),
    )
    assert saved.status_code == 200
    assert saved.json()["complete"] is True
    assert (await _action(client, headers, sr, "close_order", THEO)).status_code == 403
    closed = await _action(client, headers, sr, "close_order")
    assert closed.status_code == 200
    body = closed.json()
    assert body["status"] == "ready_for_picking"
    assert body["milestones"]["orderClosedAt"] is not None
    assert {i["asset"]["status"] for i in body["items"] if i["asset"]} == {"prepared"}


# --- FIFO candidates + assignment ------------------------------------------------------------


async def test_candidates_are_fifo(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr, stock: Stock
) -> None:
    sr = await make_staging_sr(new_types=("Tablet 11",))
    young = await stock("Tablet 11", place="T1-01", age_days=1)
    old = await stock("Tablet 11", place="T1-02", age_days=900)
    await stock("Tablet 11", location_code="HIL-STAGING", place="T1-03", age_days=950)
    await stock("Tablet 11", location_code="RIV-GOODS_IN", place=None, age_days=990)
    response = await client.get(
        f"{API}/service-requests/{sr.id}/asset-candidates",
        params={"slot": "new_base"},
        headers=await headers(VIC),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["site"]["code"] == "RIV"
    ids = [row["asset"]["id"] for row in body["data"]]
    assert ids.index(old.id) < ids.index(young.id)
    assert body["suggestedAssetId"] == ids[0]
    assert body["data"][0]["fifoRank"] == 1
    assert all(row["asset"]["location"]["code"] == "RIV-STAGING" for row in body["data"])
    old_slot = await client.get(
        f"{API}/service-requests/{sr.id}/asset-candidates",
        params={"slot": "old_base"},
        headers=await headers(VIC),
    )
    assert old_slot.status_code == 422
    assert old_slot.json()["code"] == "slot_not_assignable"


async def _assign(
    client: AsyncClient, headers: Headers, sr: ServiceRequest, serial: str, slot: str = "new_base"
) -> Any:
    return await client.post(
        f"{API}/service-requests/{sr.id}/items/{slot}/assignment",
        json={"serial": serial},
        headers=await headers(DANA),
    )


async def test_assign_verdicts(
    client: AsyncClient,
    headers: Headers,
    make_staging_sr: MakeSr,
    stock: Stock,
    db_session: AsyncSession,
) -> None:
    sr = await make_staging_sr(new_types=("Tablet 11", "Webcam 4K"))
    first = await stock("Tablet 11", place="T2-01", age_days=999)
    second = await stock("Tablet 11", place="T2-02", age_days=998)
    monitor = await stock("Monitor 24", place="T2-03")
    elsewhere = await stock("Tablet 11", location_code="HIL-STAGING", place="T2-04")
    goods_in = await stock("Tablet 11", location_code="RIV-GOODS_IN", place=None)
    unplaced = await stock("Tablet 11", place=None)
    other_sr = await make_staging_sr()
    taken = await f.make_asset(
        db_session, status=AssetStatus.ASSIGNED, device_type="Tablet 11", sr=other_sr
    )

    cases = [
        ("NOPE-404", "error", "unknown_serial"),
        (monitor.serial_number, "error", "wrong_article"),
        (taken.serial_number, "error", "not_available"),
        (elsewhere.serial_number, "warning", "wrong_location"),
        (goods_in.serial_number, "warning", "wrong_location"),
    ]
    for serial, verdict, code in cases:
        body = (await _assign(client, headers, sr, serial)).json()
        assert (body["verdict"], body["code"], body["applied"]) == (verdict, code, False), body
    # staging asset without place: still the right article and hub → no_installation_place
    unplaced.location = await f.location(db_session, "RIV-STAGING")
    await db_session.flush()
    body = (await _assign(client, headers, sr, unplaced.serial_number)).json()
    assert body["code"] == "no_installation_place"

    # not the FIFO first → warning, applied
    body = (await _assign(client, headers, sr, second.asset_tag or "")).json()
    assert (body["verdict"], body["code"], body["applied"]) == ("warning", "not_fifo", True)
    assert first.asset_tag in body["message"]
    assert body["undo"] == {
        "method": "DELETE",
        "href": f"/service-requests/{sr.id}/items/new_base/assignment",
        "body": None,
    }
    assert body["progress"] == {"done": 1, "total": 2}
    assert body["entity"]["asset"]["id"] == second.id

    # re-scan the same device → unchanged; then swap to the FIFO first → previous unassigned
    body = (await _assign(client, headers, sr, second.serial_number)).json()
    assert (body["verdict"], body["code"], body["applied"]) == ("ok", "unchanged", False)
    body = (await _assign(client, headers, sr, first.serial_number.lower())).json()
    assert (body["verdict"], body["code"]) == ("ok", "assigned")
    assert body["message"].endswith("assigned to New base device.")
    await db_session.refresh(second)
    assert second.status == AssetStatus.AVAILABLE
    assert second.service_request_id is None
    await db_session.refresh(first)
    assert first.status == AssetStatus.ASSIGNED
    assert first.service_request_id == sr.id
    movements = (
        await db_session.scalars(
            select(AssetMovement.action).where(AssetMovement.asset_id == second.id)
        )
    ).all()
    assert AssetAction.UNASSIGN in movements


async def test_assign_unknown_article(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr, stock: Stock
) -> None:
    sr = await make_staging_sr(new_types=("Tablet 11",))
    sr.items[0].article_id = None
    sr.items[0].article = None
    asset = await stock("Tablet 11", place="T3-01")
    body = (await _assign(client, headers, sr, asset.serial_number)).json()
    assert (body["verdict"], body["code"]) == ("error", "unknown_article")


async def test_assignment_http_errors(
    client: AsyncClient,
    headers: Headers,
    make_staging_sr: MakeSr,
    stock: Stock,
    db_session: AsyncSession,
) -> None:
    asset = await stock("Laptop Standard 14", place="T4-01")
    booked = await make_staging_sr(SR.APPOINTMENT_BOOKED, editor=None)
    response = await _assign(client, headers, booked, asset.serial_number)
    assert response.status_code == 409
    assert response.json()["code"] == "transition_not_allowed"

    ada = await f.user(db_session, ADA)
    foreign = await make_staging_sr(editor=ada)
    response = await _assign(client, headers, foreign, asset.serial_number)
    assert response.status_code == 409
    assert response.json()["code"] == "guard_failed"
    assert response.json()["guard"]["code"] == "is_editor"

    sr = await make_staging_sr(old_tags=("GX100001",))
    response = await _assign(client, headers, sr, asset.serial_number, slot="old_base")
    assert response.status_code == 422
    assert response.json()["code"] == "slot_not_assignable"
    response = await _assign(client, headers, sr, asset.serial_number, slot="new_peripheral_2")
    assert response.status_code == 404
    response = await _assign(client, headers, sr, asset.serial_number, slot="bogus")
    assert response.status_code == 422
    response = await client.post(
        f"{API}/service-requests/{sr.id}/items/new_base/assignment",
        json={},
        headers=await headers(DANA),
    )
    assert response.status_code == 422
    assert "serial" in response.json()["errors"]
    response = await client.post(
        f"{API}/service-requests/{sr.id}/items/new_base/assignment",
        json={"serial": asset.serial_number},
        headers=await headers(VIC),
    )
    assert response.status_code == 403
    response = await client.post(
        f"{API}/service-requests/{sr.id}/items/new_base/assignment", json={"serial": "x"}
    )
    assert response.status_code == 401


async def test_unassign(
    client: AsyncClient,
    headers: Headers,
    make_staging_sr: MakeSr,
    stock: Stock,
    db_session: AsyncSession,
) -> None:
    sr = await make_staging_sr()
    asset = await stock("Laptop Standard 14", place="T5-01", age_days=5000)
    assert (await _assign(client, headers, sr, asset.serial_number)).json()["applied"] is True
    response = await client.delete(
        f"{API}/service-requests/{sr.id}/items/new_base/assignment", headers=await headers(DANA)
    )
    assert response.status_code == 200
    assert response.json()["asset"] is None
    await db_session.refresh(asset)
    assert asset.status == AssetStatus.AVAILABLE
    assert asset.location.type == LocationType.STAGING
    assert asset.installation_place == "T5-01"
    # idempotent
    again = await client.delete(
        f"{API}/service-requests/{sr.id}/items/new_base/assignment", headers=await headers(DANA)
    )
    assert again.status_code == 200


# --- queue ---------------------------------------------------------------------------------------


async def test_personalization_queue(
    client: AsyncClient,
    headers: Headers,
    make_staging_sr: MakeSr,
    stock: Stock,
    db_session: AsyncSession,
) -> None:
    eas = await f.site(db_session, "EAS")
    later = await make_staging_sr(
        SR.APPOINTMENT_BOOKED, editor=None, region_code="westbay", appointment=soon(9)
    )
    earlier = await make_staging_sr(
        SR.APPOINTMENT_BOOKED,
        editor=None,
        region_code="westbay",
        appointment=soon(8),
        new_types=("Tablet 11",),
    )
    mine = await make_staging_sr(region_code="westbay", appointment=soon(7))
    tablet = await stock("Tablet 11", location_code="EAS-STAGING", place="Q1-01", age_days=3000)
    params = {"siteId": eas.id, "perPage": 200}
    body = (
        await client.get(f"{API}/personalization/queue", params=params, headers=await headers(VIC))
    ).json()
    numbers = [row["number"] for row in body["data"]]
    assert numbers.index(mine.number) < numbers.index(earlier.number) < numbers.index(later.number)
    row = next(r for r in body["data"] if r["id"] == earlier.id)
    assert row["allNewItemsAssigned"] is False
    [hint] = row["stockHints"]
    assert hint["slot"] == "new_base"
    assert hint["availableCount"] >= 1
    assert hint["suggested"]["id"] == tablet.id
    assert row["availableActions"] == []  # viewer

    unclaimed = (
        await client.get(
            f"{API}/personalization/queue",
            params={**params, "status": "appointment_booked"},
            headers=await headers(DANA),
        )
    ).json()
    assert {r["status"] for r in unclaimed["data"]} == {"appointment_booked"}
    assert (
        "claim" in next(r for r in unclaimed["data"] if r["id"] == earlier.id)["availableActions"]
    )
    me = (
        await client.get(
            f"{API}/personalization/queue",
            params={**params, "editorId": "me"},
            headers=await headers(DANA),
        )
    ).json()
    assert mine.id in {r["id"] for r in me["data"]}
    assert all(r["editor"]["email"] == DANA for r in me["data"])
    by_number = (
        await client.get(
            f"{API}/personalization/queue",
            params={**params, "q": earlier.number, "sort": "-number"},
            headers=await headers(DANA),
        )
    ).json()
    assert [r["id"] for r in by_number["data"]] == [earlier.id]

    for bad in ({"siteId": eas.id, "status": "shipped"}, {}, {"siteId": eas.id, "sort": "x"}):
        response = await client.get(
            f"{API}/personalization/queue", params=bad, headers=await headers(DANA)
        )
        assert response.status_code == 422, bad
    missing = await client.get(
        f"{API}/personalization/queue", params={"siteId": 9999}, headers=await headers(DANA)
    )
    assert missing.status_code == 404


# --- staging options + checklist -----------------------------------------------------------------


async def test_patch_staging(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr, db_session: AsyncSession
) -> None:
    sr = await make_staging_sr(requested_software=["Office Suite"])
    url = f"{API}/service-requests/{sr.id}/staging"
    response = await client.patch(
        url,
        json={"osReinstall": "on_pickup", "addedSoftware": ["Diagram Tool", "diagram tool"]},
        headers=await headers(DANA),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["osReinstall"] == "on_pickup"
    assert body["addedSoftware"] == ["Diagram Tool"]
    stale = await client.patch(
        url, json={"osReinstall": None, "version": 1}, headers=await headers(DANA)
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "version_conflict"
    cleared = await client.patch(
        url, json={"osReinstall": None, "version": body["version"]}, headers=await headers(DANA)
    )
    assert cleared.json()["osReinstall"] is None
    assert cleared.json()["addedSoftware"] == ["Diagram Tool"]
    ada_try = await client.patch(url, json={"osReinstall": "on_pickup"}, headers=await headers(ADA))
    assert ada_try.status_code == 409
    assert ada_try.json()["guard"]["code"] == "is_editor"
    assert (
        await client.patch(url, json={"bogus": 1}, headers=await headers(DANA))
    ).status_code == 422
    assert (
        await client.patch(url, json={"osReinstall": "later"}, headers=await headers(DANA))
    ).status_code == 422
    assert (await client.patch(url, json={}, headers=await headers(THEO))).status_code == 403

    closed = await f.make_sr(db_session, status=SR.READY_FOR_PICKING)
    locked = await client.patch(
        f"{API}/service-requests/{closed.id}/staging",
        json={"osReinstall": OsReinstallTiming.ON_DELIVERY.value},
        headers=await headers(DANA),
    )
    assert locked.status_code == 409


async def test_software_checklist(
    client: AsyncClient, headers: Headers, make_staging_sr: MakeSr, db_session: AsyncSession
) -> None:
    sr = await make_staging_sr(
        SR.PERSONALIZATION_DONE, editor=None, requested_software=["Office Suite", "VPN Client"]
    )
    url = f"{API}/service-requests/{sr.id}/software-checklist"
    body = (await client.get(url, headers=await headers(VIC))).json()
    assert body == {
        "required": ["Office Suite", "VPN Client"],
        "installed": [],
        "added": [],
        "missing": ["Office Suite", "VPN Client"],
        "complete": False,
    }
    put = await client.put(
        url,
        json={"installedSoftware": ["Office Suite"], "addedSoftware": ["Statistics Package"]},
        headers=await headers(DANA),
    )
    assert put.json()["missing"] == ["VPN Client", "Statistics Package"]
    assert (
        await client.put(
            url, json={"installedSoftware": [], "addedSoftware": []}, headers=await headers(THEO)
        )
    ).status_code == 403
    assert (
        await client.put(url, json={"installedSoftware": []}, headers=await headers(DANA))
    ).status_code == 422
    booked = await make_staging_sr(SR.APPOINTMENT_BOOKED, editor=None)
    response = await client.put(
        f"{API}/service-requests/{booked.id}/software-checklist",
        json={"installedSoftware": [], "addedSoftware": []},
        headers=await headers(DANA),
    )
    assert response.status_code == 409
    assert (
        await client.get(
            f"{API}/service-requests/999999/software-checklist", headers=await headers(VIC)
        )
    ).status_code == 404
