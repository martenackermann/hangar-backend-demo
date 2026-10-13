"""Stock matrix, buffers, recalculation and export (API.md §8.1, SPEC §6.7/§6.8, #18)."""

from __future__ import annotations

from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import JobKind, JobStatus
from app.core.models import ArticleSiteSetting, BackgroundJob
from app.core.stock_math import order_qty
from app.modules.h5_stock.services.common import RECALC_TASK
from app.modules.h5_stock.services.stock import run_recalculation
from app.testing import factories as f

from .helpers import DANA, THEO, VIC, Headers, deferred, get_json


async def test_matrix_shape_and_formula(client: AsyncClient, headers: Headers) -> None:
    body = await get_json(client, "/api/v1/stock/matrix", await headers(VIC))
    assert [s["code"] for s in body["sites"]] == ["RIV", "HIL", "EAS"]
    assert body["calculatedAt"] is not None
    assert body["meta"]["recalculating"] is False
    row = body["rows"][0]
    assert set(row["article"]) >= {"id", "number", "name", "isActive", "internalDescription"}
    assert len(row["cells"]) == 3
    for row in body["rows"]:
        for cell in row["cells"]:
            assert cell["stock"] == cell["goodsIn"] + cell["staging"] + cell["goodsOut"]
            # SPEC_RESOLUTIONS #18: max(0, demand + buffer − stock)
            assert cell["orderQty"] == max(0, cell["demand"] + cell["buffer"] - cell["stock"])
        assert row["totals"]["orderQty"] == sum(c["orderQty"] for c in row["cells"])
    assert all(r["article"]["isActive"] for r in body["rows"])
    # the story's unknown device type shows up as unmatched demand
    assert any(u["requestedType"] == "Laptop Ultra 15" for u in body["meta"]["unmatchedDemand"])
    assert body["meta"]["unresolvedDemand"] > 0


async def test_matrix_filters(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    auth = await headers(DANA)
    hil = await f.site(db_session, "HIL")
    one = await get_json(client, "/api/v1/stock/matrix", auth, siteId=str(hil.id))
    assert [s["code"] for s in one["sites"]] == ["HIL"]
    assert all(len(r["cells"]) == 1 for r in one["rows"])
    laptops = await get_json(client, "/api/v1/stock/matrix", auth, category="laptop,monitor")
    assert {r["article"]["category"] for r in laptops["rows"]} == {"laptop", "monitor"}
    search = await get_json(client, "/api/v1/stock/matrix", auth, q="Dock T5")
    assert [r["article"]["number"] for r in search["rows"]] == ["A-10102"]
    short = await get_json(client, "/api/v1/stock/matrix", auth, onlyShortfall="true")
    assert short["rows"]
    for row in short["rows"]:
        assert any(c["orderQty"] > 0 or c["demand"] > c["stock"] for c in row["cells"])
    inactive = await get_json(client, "/api/v1/stock/matrix", auth, includeInactive="true")
    assert any(not r["article"]["isActive"] for r in inactive["rows"])
    bad = await client.get("/api/v1/stock/matrix", headers=auth, params={"siteId": "999"})
    assert bad.status_code == 422
    bad_category = await client.get(
        "/api/v1/stock/matrix", headers=auth, params={"category": "spaceship"}
    )
    assert bad_category.status_code == 422


async def test_seeded_story_shortfalls(client: AsyncClient, headers: Headers) -> None:
    body = await get_json(client, "/api/v1/stock/matrix", await headers(DANA))
    by_type = {r["article"]["internalDescription"]: r for r in body["rows"]}
    eas = next(s["id"] for s in body["sites"] if s["code"] == "EAS")
    ultrawide = next(c for c in by_type["Monitor 34 Ultrawide"]["cells"] if c["siteId"] == eas)
    assert ultrawide["demand"] > ultrawide["stock"]


async def test_buffers_patch(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    riv = await f.site(db_session, "RIV")
    article = await f.article(db_session, "Docking Station USB-C")
    response = await client.patch(
        "/api/v1/stock/buffers",
        headers=await headers(DANA),
        json={"siteId": riv.id, "buffers": [{"articleId": article.id, "bufferQty": 9}]},
    )
    assert response.status_code == 200, response.text
    [cell] = response.json()["data"]
    assert cell["articleId"] == article.id
    assert cell["buffer"] == 9
    assert cell["orderQty"] == order_qty(cell["demand"], cell["stock"], 9)
    setting = (
        await db_session.scalars(
            select(ArticleSiteSetting).where(
                ArticleSiteSetting.site_id == riv.id, ArticleSiteSetting.article_id == article.id
            )
        )
    ).one()
    assert setting.buffer_qty == 9


async def test_buffers_validation_and_permissions(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    riv = await f.site(db_session, "RIV")
    cmp_ = await f.site(db_session, "CMP")
    article = await f.article(db_session)
    body = {"siteId": riv.id, "buffers": [{"articleId": article.id, "bufferQty": 3}]}
    assert (
        await client.patch("/api/v1/stock/buffers", headers=await headers(VIC), json=body)
    ).status_code == 403
    assert (
        await client.patch("/api/v1/stock/buffers", headers=await headers(THEO), json=body)
    ).status_code == 403
    assert (await client.patch("/api/v1/stock/buffers", json=body)).status_code == 401
    negative = await client.patch(
        "/api/v1/stock/buffers",
        headers=await headers(DANA),
        json={"siteId": riv.id, "buffers": [{"articleId": article.id, "bufferQty": -1}]},
    )
    assert negative.status_code == 422
    assert "buffers.0.bufferQty" in negative.json()["errors"]
    campus = await client.patch(
        "/api/v1/stock/buffers",
        headers=await headers(DANA),
        json={"siteId": cmp_.id, "buffers": [{"articleId": article.id, "bufferQty": 1}]},
    )
    assert campus.status_code == 422
    unknown = await client.patch(
        "/api/v1/stock/buffers",
        headers=await headers(DANA),
        json={"siteId": riv.id, "buffers": [{"articleId": 99999, "bufferQty": 1}]},
    )
    assert unknown.status_code == 422
    assert "buffers.0.articleId" in unknown.json()["errors"]
    extra = await client.patch(
        "/api/v1/stock/buffers", headers=await headers(DANA), json={**body, "foo": 1}
    )
    assert extra.status_code == 422


async def test_recalculate_now_defers_job(
    client: AsyncClient, headers: Headers, db_session: AsyncSession, jobs: InMemoryConnector
) -> None:
    assert (
        await client.post("/api/v1/stock/recalculate", headers=await headers(VIC), json={})
    ).status_code == 403
    response = await client.post("/api/v1/stock/recalculate", headers=await headers(DANA), json={})
    assert response.status_code == 202, response.text
    job_id = response.json()["jobId"]
    assert response.json()["status"] == "queued"
    job = await db_session.get(BackgroundJob, job_id)
    assert job is not None
    assert job.kind == JobKind.STOCK_RECALCULATE
    assert job.status == JobStatus.QUEUED
    [task] = deferred(jobs, RECALC_TASK)
    assert task["args"] == {"job_id": job_id}
    matrix = await get_json(client, "/api/v1/stock/matrix", await headers(DANA))
    assert matrix["meta"]["recalculating"] is True


async def test_run_recalculation_counts_stock(db_session: AsyncSession) -> None:
    hil = await f.site(db_session, "HIL")
    article = await f.article(db_session, "Laptop Performance 16")
    before = (
        await db_session.scalars(
            select(ArticleSiteSetting).where(
                ArticleSiteSetting.site_id == hil.id, ArticleSiteSetting.article_id == article.id
            )
        )
    ).one()
    stock_before = before.stock_qty
    await f.make_asset(
        db_session, location_code="HIL-GOODS_IN", device_type="Laptop Performance 16"
    )
    result = await run_recalculation(db_session)
    assert result["kind"] == "stock_recalculate"
    assert result["sites"] == 3
    assert result["articles"] >= 40
    await db_session.refresh(before)
    assert before.stock_qty == stock_before + 1
    assert before.stock_goods_in >= 1


async def test_export_csv(client: AsyncClient, headers: Headers, db_session: AsyncSession) -> None:
    assert (
        await client.post("/api/v1/stock/export", headers=await headers(VIC))
    ).status_code == 403
    response = await client.post("/api/v1/stock/export", headers=await headers(DANA))
    assert response.status_code == 200, response.text
    link = response.json()
    assert link["filename"].startswith("stock-matrix-")
    assert link["contentType"] == "text/csv"
    download = await client.get("/api/v1" + link["url"].split("/api/v1", 1)[1])
    assert download.status_code == 200
    text = download.content.decode("utf-8")
    assert text.startswith("﻿Hub,Article number,Article,Model number,Device type")
    assert "Riverton Hub" in text
