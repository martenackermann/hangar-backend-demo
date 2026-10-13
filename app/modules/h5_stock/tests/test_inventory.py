"""Inventories: open, scan verdicts (#25), reconciliation (#27), close/cancel, Campus (#28)."""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import SEED_INVENTORY_IDS
from app.core.enums import AssetStatus
from app.core.models import Asset, AssetMovement
from app.core.notifications import render
from app.core.timeutil import business_today
from app.modules.h5_stock.services.common import RECALC_TASK
from app.testing import factories as f

from .helpers import DANA, TARA, THEO, VIC, Headers, deferred, deliver, get_json, queued_mails

URL = "/api/v1/inventories"


async def _open(client: AsyncClient, auth: dict[str, str], site_id: int) -> dict:
    response = await client.post(URL, headers=auth, json={"siteId": site_id, "note": "Test count"})
    assert response.status_code == 201, response.text
    return response.json()


async def _scan(
    client: AsyncClient,
    auth: dict[str, str],
    inventory_id: int,
    serial: str,
    zone: int | None = None,
) -> dict:
    body: dict = {"serial": serial}
    if zone is not None:
        body["stockLocationId"] = zone
    response = await client.post(f"{URL}/{inventory_id}/scans", headers=auth, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def test_open_rules(client: AsyncClient, headers: Headers, db_session: AsyncSession) -> None:
    hil = await f.site(db_session, "HIL")
    riv = await f.site(db_session, "RIV")
    already = await client.post(URL, headers=await headers(DANA), json={"siteId": hil.id})
    assert already.status_code == 409
    assert already.json()["code"] == "inventory_already_open"
    assert (
        await client.post(URL, headers=await headers(THEO), json={"siteId": riv.id})
    ).status_code == 403
    assert (
        await client.post(URL, headers=await headers(DANA), json={"siteId": 999})
    ).status_code == 422
    opened = await _open(client, await headers(DANA), riv.id)
    assert opened["status"] == "open"
    assert opened["availableActions"] == ["close", "cancel"]
    assert {z["type"] for z in opened["zones"]} == {"goods_in", "staging", "goods_out", "return"}
    assert opened["progress"]["scanned"] == 0
    assert opened["progress"]["expected"] == opened["summary"]["expected"] > 0
    assert opened["summary"]["missing"] == opened["summary"]["expected"]


async def test_scan_verdicts(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    riv = await f.site(db_session, "RIV")
    inventory = await _open(client, await headers(DANA), riv.id)
    iid = inventory["id"]
    staging = await f.location(db_session, "RIV-STAGING")
    hil_staging = await f.location(db_session, "HIL-STAGING")
    here = await f.make_asset(db_session, location_code="RIV-STAGING", place="B4-12")
    goods_in = await f.make_asset(db_session, location_code="RIV-GOODS_IN", place=None)
    elsewhere = await f.make_asset(db_session, location_code="HIL-STAGING", place="A1-01")
    lost = await f.make_asset(db_session, status=AssetStatus.MISSING, location_code="RIV-STAGING")
    auth = await headers(THEO)  # technicians scan

    ok = await _scan(client, auth, iid, here.serial_number.lower())
    assert (ok["verdict"], ok["code"], ok["applied"]) == ("ok", "matched", True)
    assert ok["message"] == "Aero 14 G3 Laptop · Staging B4-12"
    assert ok["entity"]["result"] == "matched"
    assert ok["undo"] == {
        "method": "DELETE",
        "href": f"/inventories/{iid}/scans/{ok['entity']['id']}",
        "body": None,
    }
    assert ok["progress"]["total"] == inventory["summary"]["expected"] + 3
    assert ok["progress"]["done"] == 1

    assert here.asset_tag is not None
    duplicate = await _scan(client, auth, iid, here.asset_tag)  # tag resolves to the same asset
    assert (duplicate["verdict"], duplicate["code"], duplicate["applied"]) == (
        "warning",
        "duplicate",
        False,
    )
    assert "Theo Technician" in duplicate["message"]
    assert duplicate["undo"] is None

    zone = await _scan(client, auth, iid, goods_in.serial_number, staging.id)
    assert (zone["verdict"], zone["code"]) == ("warning", "wrong_zone")
    assert zone["message"] == "Recorded in Goods in, scanned in Staging."
    assert zone["entity"]["result"] == "wrong_location"

    site = await _scan(client, auth, iid, elsewhere.serial_number)
    assert (site["verdict"], site["code"]) == ("warning", "wrong_site")
    assert site["message"] == "Recorded at Hillford · Staging."

    unknown = await _scan(client, auth, iid, "kd-not-there")
    assert (unknown["verdict"], unknown["code"]) == ("warning", "unknown_recorded")
    assert unknown["scannedValue"] == "KD-NOT-THERE"
    assert unknown["entity"]["asset"] is None

    found = await _scan(client, auth, iid, lost.serial_number)
    assert found["code"] == "matched"
    assert "found again" in found["message"]

    bad_zone = await client.post(
        f"{URL}/{iid}/scans", headers=auth, json={"serial": "X", "stockLocationId": hil_staging.id}
    )
    assert bad_zone.status_code == 422
    blank = await client.post(f"{URL}/{iid}/scans", headers=auth, json={"serial": "   "})
    assert blank.status_code == 422
    viewer = await client.post(
        f"{URL}/{iid}/scans", headers=await headers(VIC), json={"serial": "X"}
    )
    assert viewer.status_code == 403

    detail = await get_json(client, f"{URL}/{iid}", auth)
    assert detail["summary"]["matched"] == 2
    assert detail["summary"]["wrongLocation"] == 2
    assert detail["summary"]["unknown"] == 1
    assert detail["availableActions"] == []  # technician cannot close
    assert len(detail["recentScans"]) == 5
    reco = await get_json(client, f"{URL}/{iid}/reconciliation", auth, category="matched")
    correctable = {i["serialNumber"]: i["correctable"] for i in reco["data"]}
    assert correctable == {here.serial_number: False, lost.serial_number: True}
    wrong = await get_json(client, f"{URL}/{iid}/reconciliation", auth, category="wrong_location")
    assert {i["serialNumber"] for i in wrong["data"]} == {
        goods_in.serial_number,
        elsewhere.serial_number,
    }
    assert all(i["correctable"] for i in wrong["data"])
    missing = await get_json(
        client, f"{URL}/{iid}/reconciliation", auth, category="missing", perPage=5
    )
    assert missing["meta"]["total"] == detail["summary"]["missing"]
    assert missing["summary"] == detail["summary"]
    scans = await get_json(client, f"{URL}/{iid}/scans", auth, result="unknown")
    assert [s["serialNumber"] for s in scans["data"]] == ["KD-NOT-THERE"]


async def test_undo_rules(client: AsyncClient, headers: Headers, db_session: AsyncSession) -> None:
    eas = await f.site(db_session, "EAS")
    inventory = await _open(client, await headers(DANA), eas.id)
    iid = inventory["id"]
    a = await f.make_asset(db_session, location_code="EAS-GOODS_IN", place=None)
    b = await f.make_asset(db_session, location_code="EAS-GOODS_IN", place=None)
    theo_scan = await _scan(client, await headers(THEO), iid, a.serial_number)
    tara_scan = await _scan(client, await headers(TARA), iid, b.serial_number)
    forbidden = await client.delete(
        f"/api/v1{tara_scan['undo']['href']}", headers=await headers(THEO)
    )
    assert forbidden.status_code == 403
    own = await client.delete(f"/api/v1{theo_scan['undo']['href']}", headers=await headers(THEO))
    assert own.status_code == 204
    manager = await client.delete(
        f"/api/v1{tara_scan['undo']['href']}", headers=await headers(DANA)
    )
    assert manager.status_code == 204
    gone = await client.delete(f"/api/v1{tara_scan['undo']['href']}", headers=await headers(DANA))
    assert gone.status_code == 404
    rescan = await _scan(client, await headers(THEO), iid, a.serial_number)
    assert rescan["code"] == "matched"  # undone scans can be repeated
    cancelled = await client.post(
        f"{URL}/{iid}/actions/cancel", headers=await headers(DANA), json={"comment": "Recount"}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    late = await client.delete(f"/api/v1{rescan['undo']['href']}", headers=await headers(DANA))
    assert late.status_code == 409
    assert late.json()["code"] == "undo_not_possible"
    closed_scan = await client.post(
        f"{URL}/{iid}/scans", headers=await headers(THEO), json={"serial": a.serial_number}
    )
    assert closed_scan.status_code == 409


async def test_close_campus_with_corrections(
    client: AsyncClient,
    headers: Headers,
    db_session: AsyncSession,
    jobs: InMemoryConnector,
    mailer,
) -> None:
    campus = await f.site(db_session, "CMP")
    ret = await f.location(db_session, "CMP-RETURN")
    seen = await f.make_asset(db_session, status=AssetStatus.COLLECTED, location_code="CMP-RETURN")
    unseen = await f.make_asset(
        db_session, status=AssetStatus.COLLECTED, location_code="CMP-RETURN"
    )
    refound = await f.make_asset(db_session, status=AssetStatus.MISSING, location_code="CMP-RETURN")
    stray = await f.make_asset(db_session, location_code="RIV-STAGING", place="F1-01")
    ids = {"seen": seen.id, "unseen": unseen.id, "refound": refound.id, "stray": stray.id}
    serials = {
        k: a.serial_number
        for k, a in (("seen", seen), ("unseen", unseen), ("refound", refound), ("stray", stray))
    }
    dana = await headers(DANA)
    inventory = await _open(client, dana, campus.id)
    iid = inventory["id"]
    assert [z["code"] for z in inventory["zones"]] == ["CMP-RETURN"]
    for key in ("seen", "refound", "stray"):
        await _scan(client, dana, iid, serials[key])
    before = await get_json(client, f"{URL}/{iid}", dana)
    future = (business_today() + timedelta(days=1)).isoformat()
    too_early = await client.post(
        f"{URL}/{iid}/actions/close",
        headers=dana,
        json={"countDate": future, "applyCorrections": True},
    )
    assert too_early.status_code == 422
    assert "countDate" in too_early.json()["errors"]
    tech = await client.post(
        f"{URL}/{iid}/actions/close",
        headers=await headers(THEO),
        json={"countDate": business_today().isoformat(), "applyCorrections": True},
    )
    assert tech.status_code == 403
    response = await client.post(
        f"{URL}/{iid}/actions/close",
        headers=dana,
        json={"countDate": business_today().isoformat(), "applyCorrections": True},
    )
    assert response.status_code == 200, response.text
    closed = response.json()
    assert closed["status"] == "closed"
    assert closed["applyCorrections"] is True
    assert closed["availableActions"] == []
    summary = closed["summary"]
    assert summary["expected"] == before["summary"]["expected"]
    assert summary["missing"] == before["summary"]["missing"] >= 1
    assert summary["corrected"] >= 3
    assets = {
        a.id: a
        for a in (
            await db_session.scalars(
                select(Asset)
                .where(Asset.id.in_(list(ids.values())))
                .execution_options(populate_existing=True)
            )
        ).all()
    }
    assert assets[ids["seen"]].status == AssetStatus.COLLECTED
    assert assets[ids["unseen"]].status == AssetStatus.MISSING
    assert assets[ids["refound"]].status == AssetStatus.COLLECTED
    assert assets[ids["stray"]].status == AssetStatus.COLLECTED
    assert assets[ids["stray"]].location_id == ret.id
    moves = (
        await db_session.scalars(
            select(AssetMovement).where(AssetMovement.source_ref == f"inventory:{iid}")
        )
    ).all()
    assert {m.action for m in moves} == {"mark_missing", "inventory_relocate"}
    # frozen: later movements do not change the reconciliation
    frozen = await get_json(client, f"{URL}/{iid}/reconciliation", dana, category="missing")
    assert unseen.serial_number in {i["serialNumber"] for i in frozen["data"]}
    assert frozen["summary"] == summary
    for kind in ("missing", "wrong_location", "unknown"):
        link = await get_json(client, f"{URL}/{iid}/reports/{kind}", await headers(VIC))
        assert link["filename"].endswith(f"{kind.replace('_', '-')}.csv")
    report = await get_json(client, f"{URL}/{iid}/reports/missing", dana)
    content = (await client.get("/api/v1" + report["url"].split("/api/v1", 1)[1])).content
    lines = content.decode("utf-8-sig").splitlines()
    assert lines[0] == (
        "Serial,Asset tag,Article,Status,Recorded location,Scanned location,Scanned by,Scanned at"
    )
    assert any(serials["unseen"] in line and "Return pending" in line for line in lines)
    [mail] = queued_mails(jobs, "inventory.report")
    assert mail["to"] == ["rollout-team@example.com"]
    assert len(mail["attachment_file_ids"]) == 3
    subject = render("inventory.report", mail["context"]).subject
    assert subject == f"Inventory Campus closed: {summary['missing']} missing, 1 misplaced"
    await deliver(mail)
    assert mailer.outbox[-1].subject == subject
    assert deferred(jobs, RECALC_TASK)
    again = await client.post(
        f"{URL}/{iid}/actions/close",
        headers=dana,
        json={"countDate": business_today().isoformat(), "applyCorrections": False},
    )
    assert again.status_code == 409


async def test_close_without_corrections_and_reports_404(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    hil_id = SEED_INVENTORY_IDS["HIL"]
    dana = await headers(DANA)
    open_report = await client.get(f"{URL}/{hil_id}/reports/missing", headers=dana)
    assert open_report.status_code == 404
    before = await get_json(client, f"{URL}/{hil_id}", dana)
    missing_assets = await get_json(
        client, f"{URL}/{hil_id}/reconciliation", dana, category="missing", perPage=1
    )
    probe = missing_assets["data"][0]["asset"]
    response = await client.post(
        f"{URL}/{hil_id}/actions/close",
        headers=dana,
        json={
            "countDate": business_today().isoformat(),
            "applyCorrections": False,
            "version": before["version"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["corrected"] is None
    asset = await db_session.get(Asset, probe["id"], populate_existing=True)
    assert asset is not None
    assert asset.status == probe["status"]  # unchanged without corrections


async def test_seeded_inventories(client: AsyncClient, headers: Headers) -> None:
    auth = await headers(VIC)
    page = await get_json(client, URL, auth)
    by_id = {i["id"]: i for i in page["data"]}
    eas, riv, hil = (by_id[SEED_INVENTORY_IDS[c]] for c in ("EAS", "RIV", "HIL"))
    assert eas["status"] == riv["status"] == "closed"
    assert hil["status"] == "open"
    assert eas["summary"]["missing"] == 2
    assert riv["summary"]["missing"] == 2
    assert riv["summary"]["corrected"] == 2
    assert hil["summary"]["wrongLocation"] == 3
    assert hil["summary"]["unknown"] == 2
    ratio = hil["summary"]["matched"] / hil["summary"]["expected"]
    assert 0.45 < ratio < 0.75
    detail = await get_json(client, f"{URL}/{SEED_INVENTORY_IDS['HIL']}", auth)
    assert detail["openedBy"]["email"] == DANA
    assert detail["recentScans"][0]["scannedBy"]["email"] == TARA
    reco = await get_json(
        client, f"{URL}/{SEED_INVENTORY_IDS['RIV']}/reconciliation", auth, category="missing"
    )
    assert len(reco["data"]) == 2
    assert all(i["asset"]["status"] == "missing" for i in reco["data"])
    only_open = await get_json(client, URL, auth, status="open")
    assert [i["id"] for i in only_open["data"]] == [SEED_INVENTORY_IDS["HIL"]]
    report = await get_json(client, f"{URL}/{SEED_INVENTORY_IDS['EAS']}/reports/unknown", auth)
    assert report["contentType"] == "text/csv"
