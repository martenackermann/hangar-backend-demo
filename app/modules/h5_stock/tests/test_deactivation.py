"""Deactivation upload (SPEC §6.10, API.md §8.4, SPEC_RESOLUTIONS #23/#24)."""

from __future__ import annotations

import io

import xlsxwriter
from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AssetStatus, LeaseReturnStatus, ServiceRequestStatus
from app.core.models import Asset, LeaseReturn, StatusTransition
from app.modules.h5_stock.services.common import RECALC_TASK
from app.testing import factories as f

from .helpers import DANA, THEO, VIC, Headers, deferred, get_json

URL = "/api/v1/deactivations"


def _csv(rows: list[str], header: str = "serial_number,asset_tag") -> bytes:
    return ("﻿" + "\r\n".join([header, *rows]) + "\r\n").encode()


async def _upload(
    client: AsyncClient,
    auth: dict[str, str],
    data: bytes,
    *,
    name: str = "returns.csv",
    reason: str = "returned_to_lessor",
    content_type: str = "text/csv",
):
    return await client.post(
        URL, headers=auth, files={"file": (name, data, content_type)}, data={"reason": reason}
    )


async def test_upload_outcomes(
    client: AsyncClient,
    headers: Headers,
    db_session: AsyncSession,
    jobs: InMemoryConnector,
) -> None:
    lease = await f.make_lease(db_session, status=LeaseReturnStatus.REMINDER_1_SENT)
    collected = await f.make_asset(
        db_session, status=AssetStatus.COLLECTED, location_code="HIL-RETURN"
    )
    collected.asset_tag = lease.asset_tag
    in_use = await f.make_asset(db_session, status=AssetStatus.IN_USE, location_code="IN_USE")
    sr = await f.make_sr(
        db_session, status=ServiceRequestStatus.PERSONALIZATION_IN_PROGRESS, assign_new=True
    )
    assigned = next(i.asset for i in sr.items if i.asset is not None)
    gone = (
        await db_session.scalars(
            select(Asset).where(Asset.status == AssetStatus.DEACTIVATED_RETURNED)
        )
    ).first()
    assert gone is not None
    await db_session.flush()
    ids = {
        "collected": collected.id,
        "in_use": in_use.id,
        "assigned": assigned.id,
        "lease": lease.id,
    }
    rows = [
        f"{collected.serial_number.lower()},",
        f" {in_use.serial_number} ,",
        f"{assigned.serial_number},",
        f"{gone.serial_number},",
        "KD0UNKNOWN1,",
        f"{collected.serial_number},",  # duplicate → de-duplicated
        ",",  # empty row ignored
    ]
    response = await _upload(client, await headers(DANA), _csv(rows))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["totalRows"] == 5
    assert result["deactivatedCount"] == 2
    assert result["alreadyDeactivatedCount"] == 1
    assert result["rejectedCount"] == 1
    assert result["notFoundCount"] == 1
    assert result["closedLeaseReturns"] == 1
    assert result["notFoundReport"]["filename"].endswith("not-found.csv")
    assert result["rejectedReport"]["filename"].endswith("rejected.csv")
    outcomes = {row["outcome"] for row in result["sample"]}
    assert outcomes == {"deactivated", "already_deactivated", "rejected", "not_found"}
    assert result["createdBy"]["email"] == DANA
    refreshed = {
        a.id: a
        for a in (
            await db_session.scalars(
                select(Asset)
                .where(Asset.id.in_(list(ids.values())))
                .execution_options(populate_existing=True)
            )
        ).all()
    }
    assert refreshed[ids["collected"]].status == AssetStatus.DEACTIVATED_RETURNED
    assert refreshed[ids["collected"]].location.code == "OFF_FLEET"
    assert refreshed[ids["in_use"]].status == AssetStatus.DEACTIVATED_RETURNED
    assert refreshed[ids["assigned"]].status == AssetStatus.ASSIGNED
    closed = await db_session.get(LeaseReturn, ids["lease"], populate_existing=True)
    assert closed is not None
    assert closed.status == LeaseReturnStatus.CLOSED
    transition = (
        await db_session.scalars(
            select(StatusTransition).where(
                StatusTransition.entity_type == "lease_return",
                StatusTransition.entity_id == ids["lease"],
            )
        )
    ).one()
    assert transition.trigger == "system"
    assert transition.payload is not None
    assert transition.payload["closeReason"] == "device_deactivated"
    rejected = await client.get("/api/v1" + result["rejectedReport"]["url"].split("/api/v1", 1)[1])
    text = rejected.content.decode("utf-8-sig")
    assert text.splitlines()[0] == "serial_number,status,service_request,reason"
    assert sr.number in text
    assert deferred(jobs, RECALC_TASK)  # debounced recalculation after commit
    history = await get_json(client, URL, await headers(VIC))
    assert history["data"][0]["id"] == result["id"]
    detail = await get_json(client, f"{URL}/{result['id']}", await headers(VIC))
    assert detail["closedLeaseReturns"] == 1
    assert detail["sample"] == result["sample"]


async def test_xlsx_upload_by_asset_tag(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    asset = await f.make_asset(db_session, status=AssetStatus.COLLECTED, location_code="CMP-RETURN")
    tag = asset.asset_tag
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})
    sheet = workbook.add_worksheet()
    sheet.write_row(0, 0, ["Serial Number", "Asset Tag"])
    sheet.write_row(1, 0, ["", tag])
    workbook.close()
    response = await _upload(
        client,
        await headers(DANA),
        buffer.getvalue(),
        name="buyout.xlsx",
        reason="bought_out",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert response.status_code == 200, response.text
    assert response.json()["deactivatedCount"] == 1
    assert response.json()["reason"] == "bought_out"
    refreshed = await db_session.get(Asset, asset.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.status == AssetStatus.DEACTIVATED_BOUGHT_OUT


async def test_upload_errors(client: AsyncClient, headers: Headers) -> None:
    auth = await headers(DANA)
    missing = await _upload(client, auth, _csv(["X1"], header="serial"))
    assert missing.status_code == 422
    assert missing.json()["code"] == "missing_columns"
    empty = await _upload(client, auth, _csv([]))
    assert empty.status_code == 400
    assert empty.json()["code"] == "empty_file"
    nothing = await _upload(client, auth, b"")
    assert nothing.status_code == 400
    wrong_type = await _upload(client, auth, b"hello", name="list.txt", content_type="text/plain")
    assert wrong_type.status_code == 415
    bad_reason = await _upload(client, auth, _csv(["X1,"]), reason="lost_in_space")
    assert bad_reason.status_code == 422
    latin = await _upload(client, auth, "serial_number\nKD\xe9\n".encode("latin-1"))
    assert latin.status_code == 400
    assert latin.json()["code"] == "encoding_unsupported"
    dupes = await _upload(client, auth, _csv(["X1,X1"], header="serial_number,serial_number"))
    assert dupes.status_code == 400
    assert dupes.json()["code"] == "duplicate_columns"
    fake_xlsx = await _upload(
        client,
        auth,
        b"not a zip",
        name="x.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert fake_xlsx.status_code == 400


async def test_permissions_and_sample(client: AsyncClient, headers: Headers) -> None:
    for persona in (VIC, THEO):
        response = await _upload(client, await headers(persona), _csv(["X1,"]))
        assert response.status_code == 403
    assert (await client.get(f"{URL}/sample", headers=await headers(VIC))).status_code == 403
    sample = await client.get(f"{URL}/sample", headers=await headers(DANA))
    assert sample.status_code == 200
    link = sample.json()
    content = (await client.get("/api/v1" + link["url"].split("/api/v1", 1)[1])).content
    lines = content.decode("utf-8-sig").splitlines()
    assert lines[0] == "serial_number,asset_tag"
    assert len(lines) == 7
    assert (await client.get(f"{URL}/9999", headers=await headers(DANA))).status_code == 404


async def test_seeded_batches(client: AsyncClient, headers: Headers) -> None:
    page = await get_json(client, URL, await headers(VIC), sort="createdAt")
    batches = page["data"]
    assert [b["id"] for b in batches[:2]] == [1, 2]
    first, second = batches[0], batches[1]
    assert first["reason"] == "returned_to_lessor"
    assert (first["totalRows"], first["deactivatedCount"], first["notFoundCount"]) == (32, 30, 2)
    assert second["reason"] == "bought_out"
    assert (second["totalRows"], second["deactivatedCount"]) == (6, 6)
    detail = await get_json(client, f"{URL}/1", await headers(VIC))
    assert detail["notFoundReport"] is not None
    assert detail["rejectedReport"] is None
    assert detail["closedLeaseReturns"] >= 1
