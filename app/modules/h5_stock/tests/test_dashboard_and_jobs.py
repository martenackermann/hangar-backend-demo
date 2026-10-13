"""Dashboard KPIs (API.md §8.6), the recalculation task wiring, subscribers, templates."""

from __future__ import annotations

from datetime import date

from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ImportJobStatus, ImportType, ServiceRequestStatus
from app.core.events import ImportFinished
from app.core.models import ServiceRequest
from app.core.notifications import render_template, templates
from app.jobs.app import job_app
from app.modules.h5_stock.models import CallOffOrder, Inventory
from app.modules.h5_stock.services.common import RECALC_TASK
from app.modules.h5_stock.subscribers import on_import_finished
from app.testing import factories as f

from .helpers import DANA, THEO, VIC, Headers, deferred, get_json

URL = "/api/v1/dashboard/kpis"


async def test_kpis_shape(client: AsyncClient, headers: Headers, db_session: AsyncSession) -> None:
    body = await get_json(client, URL, await headers(VIC))
    tiles = body["tiles"]
    open_count = await db_session.scalar(
        select(func.count()).where(
            ServiceRequest.status.not_in(
                (ServiceRequestStatus.COMPLETED, ServiceRequestStatus.CANCELLED)
            )
        )
    )
    assert tiles["openServiceRequests"] == open_count
    assert tiles["needsAttention"] > 0
    assert tiles["escalatedServiceRequests"] == 8
    assert tiles["hardwareUnavailable"] == 10
    assert tiles["rescheduleNeeded"] == 8
    assert tiles["openInventories"] == 1
    assert tiles["stockShortfallArticles"] > 0
    assert tiles["lastStockCalculationAt"] is not None
    assert "avgDaysImportToCompletion30d" in tiles
    assert (
        tiles["avgDaysImportToCompletion30d"] is None or tiles["avgDaysImportToCompletion30d"] > 0
    )
    assert len(body["stageCounts"]) == 8
    series = body["series"]
    assert len(series["completionsPerDay"]) == 14
    assert len(series["appointmentsPerDay"]) == 14
    assert series["appointmentsPerDay"][0]["date"] == body["today"]
    assert series["completionsPerDay"][-1]["date"] == body["today"]
    assert sum(d["count"] for d in series["importedPerDay"]) > 0
    assert len(body["regionLoad"]) == 9
    alerts = body["stockAlerts"]
    assert 0 < len(alerts) <= 10
    gaps = [a["demand"] - a["stock"] for a in alerts]
    assert gaps == sorted(gaps, reverse=True)
    assert body["filters"] == {"siteId": None, "regionId": None}
    date.fromisoformat(body["today"])


async def test_kpis_filters_and_errors(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    auth = await headers(THEO)
    all_ = await get_json(client, URL, auth)
    eas = await f.site(db_session, "EAS")
    westbay = await f.region(db_session, "westbay")
    by_site = await get_json(client, URL, auth, siteId=eas.id)
    assert by_site["filters"]["siteId"] == eas.id
    assert by_site["tiles"]["openServiceRequests"] < all_["tiles"]["openServiceRequests"]
    assert all(a["site"]["code"] == "EAS" for a in by_site["stockAlerts"])
    assert by_site["tiles"]["openInventories"] == 0
    by_region = await get_json(client, URL, auth, regionId=westbay.id)
    assert [r["region"]["code"] for r in by_region["regionLoad"]] == ["westbay"]
    assert (await client.get(URL, headers=auth, params={"siteId": 999})).status_code == 422
    assert (await client.get(URL, headers=auth, params={"regionId": "x"})).status_code == 422
    assert (await client.get(URL)).status_code == 401


async def test_import_finished_defers_debounced_recalc(jobs: InMemoryConnector) -> None:
    async with job_app.open_async():
        await on_import_finished(ImportFinished(1, ImportType.CONTACTS, ImportJobStatus.COMPLETED))
        assert deferred(jobs, RECALC_TASK) == []
        await on_import_finished(
            ImportFinished(2, ImportType.ASSETS_NEW, ImportJobStatus.COMPLETED)
        )
        await on_import_finished(
            ImportFinished(3, ImportType.SERVICE_REQUESTS, ImportJobStatus.COMPLETED_WITH_ERRORS)
        )
    [job] = deferred(jobs, RECALC_TASK)  # queueing lock: only one pending debounced run
    assert job["queueing_lock"] == "stock.recalculate.debounce"
    assert job["scheduled_at"] is not None


def test_task_registered_periodic() -> None:
    task = job_app.tasks[RECALC_TASK]
    assert task.lock == "stock.recalculate"
    periodic = [p for p in job_app.periodic_registry.periodic_tasks.values() if p.task is task]
    assert periodic
    assert periodic[0].cron == "20 * * * *"


async def test_template_previews(db_session: AsyncSession) -> None:
    for template_id, model in (("calloff.order", CallOffOrder), ("inventory.report", Inventory)):
        template = templates.get(template_id)
        assert template.owner == "h5"
        sample = render_template(template, template.sample_context)
        assert sample.subject
        entity_id = await db_session.scalar(select(func.min(model.id)))
        assert entity_id is not None
        assert template.build_context is not None
        context = await template.build_context(db_session, entity_id)
        rendered = render_template(template, context)
        assert "Acme IT Services" in rendered.html
    calloff = render_template(
        templates.get("calloff.order"), templates.get("calloff.order").sample_context
    )
    assert (
        calloff.subject == "Call-off order CO-EAS-20261013-01 – Eastgate Hub – delivery 16 Oct 2026"
    )


async def test_seeded_calloff_story(client: AsyncClient, headers: Headers) -> None:
    page = await get_json(client, "/api/v1/calloff-orders", await headers(DANA), perPage=50)
    orders = page["data"]
    assert len([o for o in orders if o["status"] == "sent"]) == 9
    [draft] = [o for o in orders if o["status"] == "draft"]
    assert draft["site"]["code"] == "EAS"
    detail = await get_json(client, f"/api/v1/calloff-orders/{draft['id']}", await headers(DANA))
    assert detail["lines"]
    assert detail["availableActions"] == ["send", "cancel"]
    sent = next(o for o in orders if o["status"] == "sent")
    doc = await get_json(
        client, f"/api/v1/calloff-orders/{sent['id']}/document", await headers(VIC), format="xlsx"
    )
    assert doc["filename"] == f"{sent['number']}.xlsx"
