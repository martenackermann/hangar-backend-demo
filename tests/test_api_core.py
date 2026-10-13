"""Core endpoints end-to-end (API.md §3) + permission matrix (SPEC §3.2)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Role
from app.core.files import signed_url, store_file
from app.core.permissions import PERMISSIONS, ROLE_PERMISSIONS

DANA, THEO, VIC, ADA = (
    "dana.dispatcher@example.com",
    "theo.technician@example.com",
    "vic.viewer@example.com",
    "ada.admin@example.com",
)
Headers = Callable[[str], Awaitable[dict[str, str]]]


async def test_login_me_enums_search_flow(client: AsyncClient) -> None:
    bad = await client.post("/api/v1/auth/login", json={"email": DANA, "password": "wrong"})
    assert (bad.status_code, bad.json()["code"]) == (401, "invalid_credentials")
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x"}
    )
    assert unknown.json()["detail"] == bad.json()["detail"]

    login = await client.post(
        "/api/v1/auth/login", json={"email": "Dana.Dispatcher@example.com", "password": "demo1234"}
    )
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["tokenType"] == "Bearer"
    assert body["user"]["role"] == "dispatcher"
    auth = {"Authorization": f"Bearer {body['accessToken']}"}

    me = (await client.get("/api/v1/me", headers=auth)).json()
    assert me["displayName"] == "Dana Dispatcher"
    assert me["homeSite"]["code"] == "RIV"
    assert "sr.comms" in me["permissions"]
    assert "master.manage" not in me["permissions"]

    enums = (await client.get("/api/v1/meta/enums", headers=auth)).json()
    assert enums["serviceRequestStatus"][0] == {
        "value": "imported",
        "label": "Imported",
        "description": "New from ITSM import; waiting for first mail",
        "tone": "intake",
        "order": 1,
        "stage": "imported",
        "terminal": False,
    }
    assert "stage" not in enums["deliveryType"][0]
    assert {"scanVerdict", "assetSource", "scanCode", "transitions", "permissions"} <= set(enums)
    send = next(
        t for t in enums["transitions"]["serviceRequest"] if t["action"] == "send_invitation"
    )
    assert send == {
        "action": "send_invitation",
        "label": "Send booking invitation",
        "from": ["imported"],
        "to": "invited",
        "triggers": ["user", "job"],
        "permission": "sr.comms",
        "bulk": True,
        "requiresComment": False,
        "destructive": False,
        "owner": "h1",
        "internal": False,
        "viaEndpoint": True,
    }
    claim = next(t for t in enums["transitions"]["serviceRequest"] if t["action"] == "claim")
    assert claim["bulk"] is False  # SPEC_RESOLUTIONS #17
    assert next(c for c in enums["scanCode"] if c["value"] == "duplicate")["verdict"] == "warning"

    exact = (await client.get("/api/v1/search", params={"q": "sr0012001"}, headers=auth)).json()
    assert exact["exact"]["title"] == "SR0012001"
    assert exact["exact"]["href"].startswith("/service-requests?sr=")
    tag = (await client.get("/api/v1/search", params={"q": "GX100001"}, headers=auth)).json()
    assert tag["exact"] is not None
    short = await client.get("/api/v1/search", params={"q": "x"}, headers=auth)
    assert short.status_code == 422
    assert (await client.post("/api/v1/auth/logout", headers=auth)).status_code == 204


async def test_personas_and_persona_login(client: AsyncClient) -> None:
    personas = (await client.get("/api/v1/auth/personas")).json()["data"]
    assert [p["email"] for p in personas][:4] == [DANA, THEO, VIC, ADA]
    assert [p["group"] for p in personas].count("more_technicians") == 3
    assert personas[0]["initials"] == "DD"
    login = await client.post("/api/v1/auth/persona-login", json={"email": THEO})
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "technician"


async def test_meta_config_is_public(client: AsyncClient) -> None:
    config = (await client.get("/api/v1/meta/config")).json()
    assert config["appName"] == "Hangar"
    assert config["businessTimeZone"] == "Europe/Zurich"
    assert config["gateways"] == {"booking": "fake", "carrier": "fake", "mailer": "memory"}
    assert config["limits"]["perPageMax"] == 200


async def test_reference_lists(client: AsyncClient, headers: Headers) -> None:
    auth = await headers(VIC)
    sites = (await client.get("/api/v1/sites", headers=auth)).json()["data"]
    assert [s["code"] for s in sites] == ["RIV", "HIL", "EAS", "CMP"]
    assert sites[0]["address"]["country"] == "Freeland"
    regions = (await client.get("/api/v1/regions", headers=auth)).json()["data"]
    assert len(regions) == 9
    assert regions[0]["ruleCount"] >= 2
    assert "openServiceRequests" in regions[0]
    locations = (
        await client.get("/api/v1/stock-locations", params={"type": "return,in_use"}, headers=auth)
    ).json()["data"]
    assert {loc["code"] for loc in locations} == {
        "RIV-RETURN",
        "HIL-RETURN",
        "EAS-RETURN",
        "CMP-RETURN",
        "IN_USE",
    }
    techs = (await client.get("/api/v1/users", params={"role": "technician"}, headers=auth)).json()[
        "data"
    ]
    assert len(techs) == 4
    assert all(t["isDemoPersona"] for t in techs)
    codes = (await client.get("/api/v1/deviation-codes", headers=auth)).json()["data"]
    assert [c["code"] for c in codes][:2] == ["DV01", "DV02"]
    assert len(codes) == 16
    bad = await client.get("/api/v1/stock-locations", params={"type": "attic"}, headers=auth)
    assert bad.status_code == 422
    assert "type" in bad.json()["errors"]


async def test_saved_views(client: AsyncClient, headers: Headers) -> None:
    vic = await headers(VIC)
    listed = (
        await client.get("/api/v1/saved-views", params={"route": "/service-requests"}, headers=vic)
    ).json()["data"]
    assert {v["name"] for v in listed} >= {"Needs attention"}
    assert all(not v["isOwn"] for v in listed)
    shared = await client.post(
        "/api/v1/saved-views",
        json={"route": "/assets", "name": "Mine", "search": {}, "isShared": True},
        headers=vic,
    )
    assert shared.status_code == 403
    created = await client.post(
        "/api/v1/saved-views",
        json={"route": "/assets", "name": "Mine", "search": {"q": "x"}},
        headers=vic,
    )
    assert created.status_code == 201
    assert created.json()["isOwn"] is True
    duplicate = await client.post(
        "/api/v1/saved-views", json={"route": "/assets", "name": "Mine", "search": {}}, headers=vic
    )
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "duplicate")
    view_id = created.json()["id"]
    renamed = await client.patch(
        f"/api/v1/saved-views/{view_id}", json={"name": "Mine 2"}, headers=vic
    )
    assert renamed.json()["name"] == "Mine 2"
    other = await client.patch(
        f"/api/v1/saved-views/{listed[0]['id']}", json={"name": "x"}, headers=vic
    )
    assert other.status_code == 403
    assert (await client.delete(f"/api/v1/saved-views/{view_id}", headers=vic)).status_code == 204


async def test_signed_file_download(client: AsyncClient, db_session: AsyncSession) -> None:
    stored = await store_file(db_session, b"%PDF-1.7 demo", "label.pdf", "application/pdf", "label")
    link = signed_url(stored)
    path = link.url.split("/api/v1", 1)[1]
    ok = await client.get(f"/api/v1{path}")
    assert ok.status_code == 200
    assert ok.content == b"%PDF-1.7 demo"
    assert ok.headers["content-disposition"].startswith("attachment;")
    tampered = await client.get(f"/api/v1{path[:-4]}abcd")
    assert (tampered.status_code, tampered.json()["code"]) == (403, "invalid_signature")
    expired = signed_url(stored, ttl=timedelta(seconds=-5))
    assert (await client.get("/api/v1" + expired.url.split("/api/v1", 1)[1])).status_code == 403
    assert link.expires_at.timestamp() > time.time()


async def test_jobs_and_demo_reset(client: AsyncClient, headers: Headers) -> None:
    viewer = await client.post("/api/v1/demo/reset", headers=await headers(VIC))
    assert viewer.status_code == 403
    accepted = await client.post("/api/v1/demo/reset", headers=await headers(ADA))
    assert accepted.status_code == 202
    job_id = accepted.json()["jobId"]
    assert accepted.json()["status"] == "queued"
    job = (await client.get(f"/api/v1/jobs/{job_id}", headers=await headers(ADA))).json()
    assert job["kind"] == "demo_reset"
    assert job["status"] == "queued"
    assert job["createdBy"]["role"] == "admin"
    mine = (
        await client.get(
            "/api/v1/jobs",
            params={"mine": "true", "status": "queued,running"},
            headers=await headers(ADA),
        )
    ).json()
    assert [j["id"] for j in mine["data"]] == [job_id]
    assert (await client.get("/api/v1/jobs/999999", headers=await headers(ADA))).status_code == 404


async def test_health(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/healthz")).json() == {"status": "ok"}
    assert (await client.get("/api/v1/readyz")).json() == {
        "status": "ok",
        "db": "ok",
        "queue": "ok",
    }


async def test_problem_details(client: AsyncClient, headers: Headers) -> None:
    missing = await client.get("/api/v1/me")
    assert missing.status_code == 401
    assert missing.headers["content-type"] == "application/problem+json"
    body = missing.json()
    assert body["type"] == "https://hangar.example.com/problems/unauthenticated"
    assert body["code"] == "unauthenticated"
    assert body["instance"] == "/api/v1/me"
    assert "detail" in body
    unknown = await client.get("/api/v1/nope")
    assert (unknown.status_code, unknown.json()["code"]) == (404, "not_found")
    invalid = await client.post(
        "/api/v1/saved-views",
        json={"route": "/x", "name": "n", "search": {}, "extra": 1},
        headers=await headers(VIC),
    )
    assert invalid.status_code == 422
    assert invalid.json()["errors"] == {"extra": ["Unknown field."]}


# --- permission matrix (SPEC §3.2) ----------------------------------------------------------

EXPECTED_ONLY = {
    "master.manage": {Role.ADMIN},
    "users.manage": {Role.ADMIN},
    "demo.reset": {Role.ADMIN},
    "comms.read": {Role.VIEWER, Role.DISPATCHER, Role.ADMIN},
    "imports.read": {Role.VIEWER, Role.DISPATCHER, Role.ADMIN},
    "staging.scan": {Role.TECHNICIAN, Role.DISPATCHER, Role.ADMIN},
    "inventory.scan": {Role.TECHNICIAN, Role.DISPATCHER, Role.ADMIN},
    "tours.execute_own": {Role.TECHNICIAN, Role.DISPATCHER, Role.ADMIN},
    "tours.execute_any": {Role.DISPATCHER, Role.ADMIN},
    "sr.read": set(Role),
    "sr.comms": {Role.DISPATCHER, Role.ADMIN},
}


@pytest.mark.parametrize(("key", "roles"), list(EXPECTED_ONLY.items()))
def test_permission_matrix(key: str, roles: set[Role]) -> None:
    assert {role for role in Role if key in ROLE_PERMISSIONS[role]} == roles


def test_matrix_shape() -> None:
    assert len(PERMISSIONS) == 41
    assert ROLE_PERMISSIONS[Role.ADMIN] == frozenset(PERMISSIONS)
    assert ROLE_PERMISSIONS[Role.DISPATCHER] == frozenset(PERMISSIONS) - {
        "master.manage",
        "users.manage",
        "demo.reset",
    }


@pytest.mark.parametrize(
    ("email", "status"),
    [(VIC, 403), (THEO, 403), (DANA, 403), (ADA, 202)],
)
async def test_403_per_role_demo_reset(
    client: AsyncClient, headers: Headers, email: str, status: int
) -> None:
    response = await client.post("/api/v1/demo/reset", headers=await headers(email))
    assert response.status_code == status
    if status == 403:
        assert response.json()["code"] == "forbidden"
