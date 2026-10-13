"""Call-off orders: create → edit → send / cancel, documents, mail (SPEC §4.8, §6.8)."""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.files import file_store
from app.core.models import ArticleSiteSetting, StoredFile
from app.core.notifications import render
from app.core.timeutil import add_working_days, business_today
from app.modules.h5_stock.models import CallOffOrder
from app.testing import factories as f

from .helpers import ADA, DANA, THEO, VIC, Headers, deliver, get_json, queued_mails

URL = "/api/v1/calloff-orders"


def _date(days: int = 3) -> str:
    return add_working_days(business_today(), days).isoformat()


async def _create(client: AsyncClient, auth: dict[str, str], site_id: int) -> dict:
    response = await client.post(
        URL, headers=auth, json={"siteId": site_id, "deliveryDate": _date(), "note": "Ramp B"}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_draft_from_matrix(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    hil = await f.site(db_session, "HIL")
    order = await _create(client, await headers(DANA), hil.id)
    assert order["status"] == "draft"
    assert order["number"] == f"CO-HIL-{business_today():%Y%m%d}-01"
    assert order["recipientEmail"] == "partner-orders@example.com"
    assert order["availableActions"] == ["send", "cancel"]
    assert order["mailLogEntityRef"] == {
        "type": "calloff_order",
        "id": order["id"],
        "label": order["number"],
    }
    assert order["lines"]
    matrix = await get_json(client, "/api/v1/stock/matrix", await headers(DANA), siteId=str(hil.id))
    expected = {
        r["article"]["id"]: r["cells"][0]["orderQty"]
        for r in matrix["rows"]
        if r["cells"][0]["orderQty"] > 0
    }
    assert {line["article"]["id"]: line["suggestedQty"] for line in order["lines"]} == expected
    for line in order["lines"]:
        assert line["quantity"] == line["suggestedQty"]
        assert line["suggestedQty"] == max(
            0, line["demandQty"] + line["bufferQty"] - line["stockQty"]
        )
    assert order["lineCount"] == len(order["lines"])
    assert order["totalQuantity"] == sum(line["quantity"] for line in order["lines"])
    second = await _create(client, await headers(DANA), hil.id)
    assert second["number"].endswith("-02")


async def test_create_validation_and_permissions(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    riv = await f.site(db_session, "RIV")
    cmp_ = await f.site(db_session, "CMP")
    body = {"siteId": riv.id, "deliveryDate": _date()}
    assert (await client.post(URL, headers=await headers(VIC), json=body)).status_code == 403
    assert (await client.post(URL, headers=await headers(THEO), json=body)).status_code == 403
    campus = await client.post(URL, headers=await headers(DANA), json={**body, "siteId": cmp_.id})
    assert campus.status_code == 422
    assert "siteId" in campus.json()["errors"]
    past = (business_today() - timedelta(days=1)).isoformat()
    old = await client.post(URL, headers=await headers(DANA), json={**body, "deliveryDate": past})
    assert old.status_code == 422
    assert "deliveryDate" in old.json()["errors"]
    missing = await client.post(URL, headers=await headers(DANA), json={"siteId": riv.id})
    assert missing.status_code == 422


async def test_patch_lines_and_warnings(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    eas = await f.site(db_session, "EAS")
    auth = await headers(DANA)
    order = await _create(client, auth, eas.id)
    first = order["lines"][0]
    pen = await f.article(db_session, "Stylus Pen")
    laptop = await f.article(db_session, "Laptop Standard 14")
    patch = {
        "version": order["version"],
        "lines": [
            {"articleId": first["article"]["id"], "quantity": 0},
            {"articleId": pen.id, "quantity": 3},
            {"articleId": laptop.id, "quantity": 500},
        ],
        "note": "Updated",
        "recipientEmail": "orders.desk@example.com",
    }
    response = await client.patch(f"{URL}/{order['id']}", headers=auth, json=patch)
    assert response.status_code == 200, response.text
    updated = response.json()
    ids = {line["article"]["id"]: line for line in updated["lines"]}
    assert first["article"]["id"] not in ids
    assert ids[pen.id]["quantity"] == 3
    assert ids[laptop.id]["quantity"] == 500
    assert "above_max" in ids[laptop.id]["warnings"]
    assert "above_max" in updated["warnings"]
    assert updated["note"] == "Updated"
    assert updated["recipientEmail"] == "orders.desk@example.com"
    assert updated["version"] > order["version"]
    stale = await client.patch(
        f"{URL}/{order['id']}", headers=auth, json={"version": order["version"], "note": "x"}
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "version_conflict"
    dup = await client.patch(
        f"{URL}/{order['id']}",
        headers=auth,
        json={
            "lines": [{"articleId": pen.id, "quantity": 1}, {"articleId": pen.id, "quantity": 2}]
        },
    )
    assert dup.status_code == 422
    negative = await client.patch(
        f"{URL}/{order['id']}",
        headers=auth,
        json={"lines": [{"articleId": pen.id, "quantity": -1}]},
    )
    assert negative.status_code == 422
    assert "lines.0.quantity" in negative.json()["errors"]
    unknown = await client.patch(
        f"{URL}/{order['id']}", headers=auth, json={"lines": [{"articleId": 9999, "quantity": 1}]}
    )
    assert unknown.status_code == 422
    assert "lines.0.articleId" in unknown.json()["errors"]


async def test_send_order(
    client: AsyncClient,
    headers: Headers,
    db_session: AsyncSession,
    jobs: InMemoryConnector,
    mailer,
) -> None:
    riv = await f.site(db_session, "RIV")
    auth = await headers(DANA)
    order = await _create(client, auth, riv.id)
    dock = await f.article(db_session, "Docking Station Thunderbolt")
    order = (
        await client.patch(
            f"{URL}/{order['id']}",
            headers=auth,
            json={"lines": [{"articleId": dock.id, "quantity": 4}]},
        )
    ).json()
    response = await client.post(
        f"{URL}/{order['id']}/actions/send", headers=auth, json={"version": order["version"]}
    )
    assert response.status_code == 200, response.text
    sent = response.json()
    assert sent["status"] == "sent"
    assert sent["sentBy"]["email"] == DANA
    assert sent["sentAt"] is not None
    assert sent["availableActions"] == []
    row = await db_session.get(CallOffOrder, order["id"])
    assert row is not None
    assert row.xlsx_file_id is not None
    assert row.pdf_file_id is not None
    pdf = await db_session.get(StoredFile, row.pdf_file_id)
    assert pdf is not None
    assert pdf.purpose == "calloff_document"
    from io import BytesIO

    text = PdfReader(BytesIO(file_store().read(pdf.storage_key))).pages[0].extract_text()
    assert order["number"] in text
    assert "Initech Logistics" in text
    # last order date/qty updated per line, buffers untouched (legacy reset them)
    line = sent["lines"][0]
    setting = (
        await db_session.scalars(
            select(ArticleSiteSetting).where(
                ArticleSiteSetting.site_id == riv.id,
                ArticleSiteSetting.article_id == line["article"]["id"],
            )
        )
    ).one()
    assert setting.last_order_date == business_today()
    assert setting.last_ordered_qty == line["quantity"]
    assert setting.buffer_qty == line["bufferQty"]
    [mail] = queued_mails(jobs, "calloff.order")
    assert mail["to"] == ["partner-orders@example.com"]
    assert mail["cc"] == ["rollout-team@example.com"]
    assert mail["attachment_file_ids"] == [row.xlsx_file_id, row.pdf_file_id]
    assert mail["entity"] == {"type": "calloff_order", "id": order["id"], "label": order["number"]}
    rendered = render("calloff.order", mail["context"])
    assert rendered.subject.startswith(f"Call-off order {order['number']} – Riverton Hub")
    assert "Initech Logistics" in rendered.html
    await deliver(mail)
    assert mailer.outbox[-1].to == ["partner-orders@example.com"]
    again = await client.post(f"{URL}/{order['id']}/actions/send", headers=auth, json={})
    assert again.status_code == 409
    assert again.json()["code"] == "transition_not_allowed"
    locked = await client.patch(f"{URL}/{order['id']}", headers=auth, json={"note": "x"})
    assert locked.status_code == 409
    docs = await client.get(
        f"{URL}/{order['id']}/document", headers=auth, params={"format": "xlsx"}
    )
    assert docs.status_code == 200
    assert docs.json()["filename"] == f"{order['number']}.xlsx"


async def test_send_guards(client: AsyncClient, headers: Headers, db_session: AsyncSession) -> None:
    hil = await f.site(db_session, "HIL")
    auth = await headers(DANA)
    order = await _create(client, auth, hil.id)
    zero = {
        "lines": [{"articleId": line["article"]["id"], "quantity": 0} for line in order["lines"]]
    }
    emptied = await client.patch(f"{URL}/{order['id']}", headers=auth, json=zero)
    assert emptied.status_code == 200
    assert emptied.json()["warnings"] == ["zero_lines"]
    assert emptied.json()["availableActions"] == ["cancel"]
    response = await client.post(f"{URL}/{order['id']}/actions/send", headers=auth, json={})
    assert response.status_code == 409
    assert response.json()["code"] == "guard_failed"
    assert response.json()["guard"]["code"] == "no_lines"
    pen = await f.article(db_session, "Stylus Pen")
    await client.patch(
        f"{URL}/{order['id']}", headers=auth, json={"lines": [{"articleId": pen.id, "quantity": 1}]}
    )
    row = await db_session.get(CallOffOrder, order["id"])
    assert row is not None
    row.delivery_date = business_today() - timedelta(days=2)
    await db_session.flush()
    past = await client.post(f"{URL}/{order['id']}/actions/send", headers=auth, json={})
    assert past.status_code == 409
    assert past.json()["guard"]["code"] == "delivery_date_past"


async def test_cancel_and_permissions(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    eas = await f.site(db_session, "EAS")
    order = await _create(client, await headers(ADA), eas.id)
    viewer = await client.post(
        f"{URL}/{order['id']}/actions/cancel", headers=await headers(VIC), json={}
    )
    assert viewer.status_code == 403
    response = await client.post(
        f"{URL}/{order['id']}/actions/cancel",
        headers=await headers(DANA),
        json={"comment": "Ordered by phone instead"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert "Ordered by phone instead" in response.json()["note"]
    again = await client.post(
        f"{URL}/{order['id']}/actions/cancel", headers=await headers(DANA), json={}
    )
    assert again.status_code == 409


async def test_list_detail_and_draft_document(
    client: AsyncClient, headers: Headers, db_session: AsyncSession
) -> None:
    auth = await headers(VIC)
    page = await get_json(client, URL, auth)
    assert page["meta"]["total"] >= 10
    statuses = [o["status"] for o in page["data"]]
    assert "draft" in statuses
    assert "sent" in statuses
    created = [o["createdAt"] for o in page["data"]]
    assert created == sorted(created, reverse=True)
    eas = await f.site(db_session, "EAS")
    only = await get_json(client, URL, auth, siteId=str(eas.id), status="sent")
    assert only["data"]
    assert all(o["site"]["code"] == "EAS" and o["status"] == "sent" for o in only["data"])
    by_date = await get_json(client, URL, auth, sort="deliveryDate")
    dates = [o["deliveryDate"] for o in by_date["data"]]
    assert dates == sorted(dates)
    assert (await client.get(URL, headers=auth, params={"sort": "nope"})).status_code == 422
    draft = next(o for o in page["data"] if o["status"] == "draft")
    detail = await get_json(client, f"{URL}/{draft['id']}", auth)
    assert detail["availableActions"] == []  # viewer
    doc = await client.get(f"{URL}/{draft['id']}/document", headers=auth)
    assert doc.status_code == 200
    assert doc.json()["contentType"] == "application/pdf"
    assert (await client.get(f"{URL}/99999", headers=auth)).status_code == 404
