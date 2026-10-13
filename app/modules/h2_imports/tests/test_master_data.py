"""Articles, regions + rules (test, reapply), VIP list and user administration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import verify_password
from app.core.enums import ServiceRequestStatus
from app.core.events import UserDeactivated, bus
from app.core.models import Region, RegionRule, ServiceRequest, User, VipEntry
from app.modules.h2_imports.services.regions import reapply_regions
from app.testing import factories as f

DANA = "dana.dispatcher@example.com"
THEO = "theo.technician@example.com"
VIC = "vic.viewer@example.com"
ADA = "ada.admin@example.com"
Headers = Callable[[str], Awaitable[dict[str, str]]]

ARTICLE = {
    "number": "A-19001",
    "category": "laptop",
    "name": "Aero 15 Test Laptop",
    "modelNumber": "KD-AERO15-T1",
    "manufacturer": "Kestrel Devices",
    "externalPartNumber": None,
    "internalDescription": "Laptop Test 15",
    "weightKg": 1.8,
}


# --- articles ------------------------------------------------------------------------------


async def test_articles_list_filters_and_counts(client: AsyncClient, headers: Headers) -> None:
    auth = await headers(VIC)
    body = (await client.get("/api/v1/articles?perPage=100", headers=auth)).json()
    assert body["meta"]["total"] == 37  # 40 seeded, 3 inactive
    laptop = next(a for a in body["data"] if a["number"] == "A-10001")
    assert laptop["assetCount"] > 0
    assert laptop["openDemand"] > 0
    assert laptop["weightKg"] == 1.6
    inactive = (await client.get("/api/v1/articles?active=false", headers=auth)).json()
    assert {a["number"] for a in inactive["data"]} == {"A-10005", "A-10103", "A-10205"}
    monitors = (
        await client.get("/api/v1/articles?category=monitor&q=27&sort=-name", headers=auth)
    ).json()["data"]
    # name order depends on the server collation (C locally, en_US in CI), so check the set
    assert {a["number"] for a in monitors} == {"A-10203", "A-10202"}
    bad = await client.get("/api/v1/articles?sort=weight", headers=auth)
    assert bad.status_code == 422


async def test_article_crud_conflicts_and_site_settings(
    client: AsyncClient, headers: Headers
) -> None:
    auth = await headers(ADA)
    created = await client.post("/api/v1/articles", json=ARTICLE, headers=auth)
    assert created.status_code == 201, created.text
    article = created.json()
    assert len(article["siteSettings"]) == 3
    assert {s["site"]["code"] for s in article["siteSettings"]} == {"RIV", "HIL", "EAS"}

    duplicate = await client.post(
        "/api/v1/articles",
        json={**ARTICLE, "number": "A-10001", "internalDescription": "Laptop Standard 14"},
        headers=auth,
    )
    assert duplicate.status_code == 409
    assert set(duplicate.json()["errors"]) == {"number", "modelNumber", "internalDescription"}

    patched = await client.patch(
        f"/api/v1/articles/{article['id']}", json={"name": "Aero 15 Test Laptop v2"}, headers=auth
    )
    assert patched.json()["name"] == "Aero 15 Test Laptop v2"
    empty = await client.patch(
        f"/api/v1/articles/{article['id']}", json={"name": None}, headers=auth
    )
    assert empty.status_code == 422
    unknown = await client.patch(
        f"/api/v1/articles/{article['id']}", json={"colour": "red"}, headers=auth
    )
    assert unknown.status_code == 422

    riv = next(s for s in article["siteSettings"] if s["site"]["code"] == "RIV")["site"]["id"]
    setting = await client.patch(
        f"/api/v1/articles/{article['id']}/site-settings/{riv}",
        json={"minQty": 2, "maxQty": 9},
        headers=auth,
    )
    assert (setting.json()["minQty"], setting.json()["maxQty"]) == (2, 9)
    invalid = await client.patch(
        f"/api/v1/articles/{article['id']}/site-settings/{riv}",
        json={"minQty": 5, "maxQty": 1},
        headers=auth,
    )
    assert invalid.status_code == 422
    assert "maxQty" in invalid.json()["errors"]

    deactivated = await client.post(
        f"/api/v1/articles/{article['id']}/actions/deactivate", json={}, headers=auth
    )
    assert deactivated.json()["isActive"] is False
    clash = await client.post(
        "/api/v1/articles",
        json={**ARTICLE, "number": "A-19002", "modelNumber": "KD-AERO15-T2"},
        headers=auth,
    )
    assert clash.status_code == 201
    reactivate = await client.post(
        f"/api/v1/articles/{article['id']}/actions/activate", json={}, headers=auth
    )
    assert reactivate.status_code == 409


@pytest.mark.parametrize("persona", [DANA, VIC, THEO])
async def test_article_writes_need_master_manage(
    client: AsyncClient, headers: Headers, persona: str
) -> None:
    response = await client.post("/api/v1/articles", json=ARTICLE, headers=await headers(persona))
    assert response.status_code == 403
    read = await client.get("/api/v1/articles", headers=await headers(persona))
    assert read.status_code == 200


# --- regions -------------------------------------------------------------------------------


async def test_region_detail_and_patch(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    westbay = await f.region(db_session, "westbay")
    auth = await headers(ADA)
    detail = (await client.get(f"/api/v1/regions/{westbay.id}", headers=auth)).json()
    assert detail["code"] == "westbay"
    assert len(detail["rules"]) == 3  # zip prefix, city, removal rule
    assert detail["bookingLinkExample"].endswith("?ref=SR0012345")
    assert detail["openServiceRequests"] >= 1

    pickup = await client.patch(
        f"/api/v1/regions/{westbay.id}",
        json={"defaultDeliveryType": "parcel_to_pickup_point"},
        headers=auth,
    )
    assert pickup.status_code == 422
    assert "pickupPoint" in pickup.json()["errors"]
    patched = await client.patch(
        f"/api/v1/regions/{westbay.id}",
        json={
            "defaultDeliveryType": "parcel_to_pickup_point",
            "pickupPoint": {
                "name": "Westbay Office",
                "street": "2 Pier Road",
                "zip": "50100",
                "city": "Westbay",
            },
            "bookingDestinationId": None,
        },
        headers=auth,
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["pickupPoint"]["name"] == "Westbay Office"
    assert body["bookingLinkExample"] is None
    hillford = await f.site(db_session, "HIL")
    moved = await client.patch(
        f"/api/v1/regions/{westbay.id}", json={"supplyingSiteId": hillford.id}, headers=auth
    )
    assert moved.json()["supplyingSite"]["code"] == "HIL"
    campus = await f.site(db_session, "CMP")
    bad = await client.patch(
        f"/api/v1/regions/{westbay.id}", json={"supplyingSiteId": campus.id}, headers=auth
    )
    assert bad.status_code == 422
    forbidden = await client.patch(
        f"/api/v1/regions/{westbay.id}", json={"name": "X"}, headers=await headers(DANA)
    )
    assert forbidden.status_code == 403


async def test_region_rules_crud_and_duplicates(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    oakridge = await f.region(db_session, "oakridge")
    auth = await headers(ADA)
    created = await client.post(
        "/api/v1/region-rules",
        json={
            "regionId": oakridge.id,
            "matchKind": "zip",
            "pattern": "9-1234",
            "procurementType": None,
            "deliveryType": "technician",
            "isCampus": False,
            "priority": 5,
            "isActive": True,
            "note": "Office park",
        },
        headers=auth,
    )
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["normalizedPattern"] == "91234"
    assert rule["region"]["code"] == "oakridge"
    duplicate = await client.post(
        "/api/v1/region-rules",
        json={"regionId": oakridge.id, "matchKind": "zip", "pattern": "91234"},
        headers=auth,
    )
    assert duplicate.status_code == 409
    bad_zip = await client.post(
        "/api/v1/region-rules",
        json={"regionId": oakridge.id, "matchKind": "zip", "pattern": "12"},
        headers=auth,
    )
    assert bad_zip.status_code == 422
    no_pickup = await client.post(
        "/api/v1/region-rules",
        json={
            "regionId": oakridge.id,
            "matchKind": "city",
            "pattern": "Oak Hill",
            "deliveryType": "parcel_to_pickup_point",
        },
        headers=auth,
    )
    assert no_pickup.status_code == 422
    assert "deliveryType" in no_pickup.json()["errors"]

    listing = (
        await client.get(f"/api/v1/region-rules?regionId={oakridge.id}&matchKind=zip", headers=auth)
    ).json()["data"]
    assert [r["id"] for r in listing] == [rule["id"]]
    patched = await client.patch(
        f"/api/v1/region-rules/{rule['id']}", json={"priority": 7, "isActive": False}, headers=auth
    )
    assert (patched.json()["priority"], patched.json()["isActive"]) == (7, False)
    deleted = await client.delete(f"/api/v1/region-rules/{rule['id']}", headers=auth)
    assert deleted.status_code == 204
    assert await db_session.get(RegionRule, rule["id"]) is None


async def test_resolver_tester(client: AsyncClient, headers: Headers) -> None:
    auth = await headers(VIC)
    campus = (
        await client.post(
            "/api/v1/region-rules/test", json={"zip": "10500", "city": "Riverton"}, headers=auth
        )
    ).json()
    assert campus["resolved"] is True
    assert campus["isCampus"] is True
    assert campus["region"]["code"] == "riverton"
    assert campus["deliveryType"] == "technician"
    assert [c["rank"] for c in campus["candidates"]] == [1, 2, 3]
    assert campus["candidates"][0]["rule"]["matchKind"] == "zip"
    removal = (
        await client.post(
            "/api/v1/region-rules/test",
            json={"zip": "50123", "procurementType": "removal"},
            headers=auth,
        )
    ).json()
    assert removal["region"]["code"] == "westbay"
    assert removal["deliveryType"] == "technician"
    assert removal["rule"]["procurementType"] == "removal"
    none = (
        await client.post("/api/v1/region-rules/test", json={"zip": "99001"}, headers=auth)
    ).json()
    assert none["resolved"] is False
    assert none["candidates"] == []
    assert none["normalizedInput"] == {"zip": "99001", "city": None, "costCenter": None}


async def test_reapply_job_and_function(
    db_session: AsyncSession, client: AsyncClient, headers: Headers, jobs: InMemoryConnector
) -> None:
    response = await client.post(
        "/api/v1/region-rules/reapply", json={}, headers=await headers(ADA)
    )
    assert response.status_code == 202
    [task] = [j for j in jobs.jobs.values() if j["task_name"] == "regions.reapply"]
    assert task["args"] == {"job_id": response.json()["jobId"]}
    forbidden = await client.post(
        "/api/v1/region-rules/reapply", json={}, headers=await headers(DANA)
    )
    assert forbidden.status_code == 403

    sr = await f.make_sr(
        db_session,
        status=ServiceRequestStatus.INVITED,
        delivery_zip="20110",
        delivery_city="Hillford",
    )
    booked = await f.make_sr(
        db_session, status=ServiceRequestStatus.APPOINTMENT_BOOKED, delivery_zip="20110"
    )
    result = await reapply_regions(db_session)
    assert result["kind"] == "regions_reapply"
    assert result["serviceRequestsChanged"] >= 1
    refreshed = await db_session.get(ServiceRequest, sr.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.region is not None
    assert refreshed.region.code == "hillford"
    untouched = await db_session.get(ServiceRequest, booked.id, populate_existing=True)
    assert untouched is not None
    assert untouched.region is not None
    assert untouched.region.code == "riverton"  # RES #29: booked SRs are not re-resolved


# --- VIP list ------------------------------------------------------------------------------


async def test_vip_entries_crud_recomputes_flags(
    db_session: AsyncSession, client: AsyncClient, headers: Headers
) -> None:
    sr = await f.make_sr(db_session, status=ServiceRequestStatus.INVITED)
    user_id = sr.contact.user_id
    auth = await headers(ADA)
    listing = (await client.get("/api/v1/vip-entries", headers=await headers(VIC))).json()["data"]
    assert len(listing) == 8
    assert sum(1 for v in listing if v["openServiceRequests"] > 0) >= 6

    created = await client.post(
        "/api/v1/vip-entries",
        json={
            "userId": user_id.lower(),
            "firstName": "Test",
            "lastName": sr.contact.last_name,
            "vipType": "Executive",
            "title": "Chief Testing Officer",
            "note": None,
        },
        headers=auth,
    )
    assert created.status_code == 201, created.text
    entry = created.json()
    assert entry["userId"] == user_id
    assert entry["contact"]["userId"] == user_id
    assert entry["openServiceRequests"] == 1
    refreshed = await db_session.get(ServiceRequest, sr.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.is_vip is True
    assert refreshed.status == ServiceRequestStatus.INVITED  # flag only

    duplicate = await client.post(
        "/api/v1/vip-entries",
        json={"userId": user_id, "lastName": "X", "vipType": "Board"},
        headers=auth,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "duplicate"
    patched = await client.patch(
        f"/api/v1/vip-entries/{entry['id']}", json={"vipType": "Board"}, headers=auth
    )
    assert patched.json()["vipType"] == "Board"
    no_user_change = await client.patch(
        f"/api/v1/vip-entries/{entry['id']}", json={"userId": "E1"}, headers=auth
    )
    assert no_user_change.status_code == 422
    searched = (await client.get("/api/v1/vip-entries?q=testing", headers=auth)).json()["data"]
    assert [v["id"] for v in searched] == [entry["id"]]

    deleted = await client.delete(f"/api/v1/vip-entries/{entry['id']}", headers=auth)
    assert deleted.status_code == 204
    refreshed = await db_session.get(ServiceRequest, sr.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.is_vip is False
    assert await db_session.scalar(select(VipEntry.id).where(VipEntry.user_id == user_id)) is None
    technician = await client.get("/api/v1/vip-entries", headers=await headers(THEO))
    assert technician.status_code == 200  # master.read; the nav hides it for technicians


# --- users ---------------------------------------------------------------------------------


async def test_users_admin(db_session: AsyncSession, client: AsyncClient, headers: Headers) -> None:
    auth = await headers(ADA)
    riverton = await f.site(db_session, "RIV")
    created = await client.post(
        "/api/v1/users",
        json={
            "email": "New.Person@Example.com",
            "firstName": "New",
            "lastName": "Person",
            "role": "technician",
            "homeSiteId": riverton.id,
            "password": "long-enough-1",
        },
        headers=auth,
    )
    assert created.status_code == 201, created.text
    user = created.json()
    assert user["email"] == "new.person@example.com"
    assert user["isDemoPersona"] is False
    assert user["homeSite"]["code"] == "RIV"
    duplicate = await client.post(
        "/api/v1/users",
        json={
            **{"email": "new.person@example.com", "firstName": "A", "lastName": "B"},
            "role": "viewer",
            "homeSiteId": None,
            "password": "long-enough-1",
        },
        headers=auth,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "duplicate"
    short = await client.post(
        "/api/v1/users",
        json={
            "email": "x@example.com",
            "firstName": "A",
            "lastName": "B",
            "role": "viewer",
            "homeSiteId": None,
            "password": "short",
        },
        headers=auth,
    )
    assert short.status_code == 422

    patched = await client.patch(
        f"/api/v1/users/{user['id']}", json={"role": "dispatcher", "homeSiteId": None}, headers=auth
    )
    assert (patched.json()["role"], patched.json()["homeSite"]) == ("dispatcher", None)

    seen: list[UserDeactivated] = []

    async def capture(_session: AsyncSession, event: UserDeactivated) -> None:
        seen.append(event)

    bus.subscribe(UserDeactivated, capture, phase="in_transaction")
    try:
        deactivated = await client.post(
            f"/api/v1/users/{user['id']}/actions/deactivate", json={}, headers=auth
        )
    finally:
        bus.unsubscribe(UserDeactivated, capture)
    assert deactivated.json()["isActive"] is False
    assert [e.user_id for e in seen] == [user["id"]]
    listing = (await client.get("/api/v1/users?active=all", headers=auth)).json()["data"]
    assert any(u["id"] == user["id"] and not u["isActive"] for u in listing)
    activated = await client.post(
        f"/api/v1/users/{user['id']}/actions/activate", json={}, headers=auth
    )
    assert activated.json()["isActive"] is True

    reset = await client.post(
        f"/api/v1/users/{user['id']}/actions/reset_password",
        json={"newPassword": "another-secret-2"},
        headers=auth,
    )
    assert reset.status_code == 204
    stored = await db_session.get(User, user["id"], populate_existing=True)
    assert stored is not None
    assert verify_password("another-secret-2", stored.password_hash)
    fetched = await client.get(f"/api/v1/users/{user['id']}", headers=auth)
    assert fetched.json()["id"] == user["id"]


async def test_user_admin_guards(
    client: AsyncClient, headers: Headers, user: Callable[[str], Awaitable[User]]
) -> None:
    auth = await headers(ADA)
    ada = await user(ADA)
    theo = await user(THEO)
    own = await client.post(f"/api/v1/users/{ada.id}/actions/deactivate", json={}, headers=auth)
    assert own.status_code == 409
    own_role = await client.patch(f"/api/v1/users/{ada.id}", json={"role": "viewer"}, headers=auth)
    assert own_role.status_code == 409
    persona_role = await client.patch(
        f"/api/v1/users/{theo.id}", json={"role": "viewer"}, headers=auth
    )
    assert persona_role.status_code == 409
    persona_password = await client.post(
        f"/api/v1/users/{theo.id}/actions/reset_password",
        json={"newPassword": "something-long"},
        headers=auth,
    )
    assert persona_password.status_code == 409
    renamed = await client.patch(
        f"/api/v1/users/{theo.id}", json={"firstName": "Theodor"}, headers=auth
    )
    assert renamed.json()["firstName"] == "Theodor"
    missing = await client.get("/api/v1/users/999999", headers=auth)
    assert missing.status_code == 404


@pytest.mark.parametrize("persona", [DANA, VIC, THEO])
async def test_user_admin_needs_users_manage(
    client: AsyncClient, headers: Headers, persona: str, user: Callable[[str], Awaitable[User]]
) -> None:
    target = await user(VIC)
    auth = await headers(persona)
    responses: list[Any] = [
        await client.get(f"/api/v1/users/{target.id}", headers=auth),
        await client.post(f"/api/v1/users/{target.id}/actions/deactivate", json={}, headers=auth),
        await client.patch(f"/api/v1/users/{target.id}", json={"firstName": "X"}, headers=auth),
    ]
    assert [r.status_code for r in responses] == [403, 403, 403]
    region = await client.get("/api/v1/regions/1", headers=auth)
    assert region.status_code == 200


async def test_region_404(client: AsyncClient, headers: Headers, db_session: AsyncSession) -> None:
    highest = await db_session.scalar(select(Region.id).order_by(Region.id.desc()))
    response = await client.get(
        f"/api/v1/regions/{(highest or 0) + 100}", headers=await headers(VIC)
    )
    assert response.status_code == 404
