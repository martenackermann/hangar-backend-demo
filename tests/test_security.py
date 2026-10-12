"""Passwords, JWTs and the permission dependency."""

from collections.abc import AsyncIterator, Callable
from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import security
from app.core.errors import AuthenticationError
from app.core.security import (
    CurrentUser,
    CurrentUserDep,
    create_access_token,
    decode_access_token,
    hash_password,
    permission_matches,
    require_permission,
    verify_password,
)
from app.main import create_app

router = APIRouter(prefix="/_test", tags=["test"])


@router.get("/me")
async def me(user: CurrentUserDep) -> dict[str, object]:
    return {"id": user.id, "roles": sorted(user.roles)}


@router.post("/break")
async def break_things(
    user: Annotated[CurrentUser, Depends(require_permission("doors.break"))],
) -> dict[str, str]:
    return {"by": user.id}


@router.post("/either")
async def either(
    _: Annotated[CurrentUser, Depends(require_permission("a.x", "b.y", any_of=True))],
) -> dict[str, bool]:
    return {"ok": True}


@pytest.fixture(scope="module")
def security_app() -> FastAPI:
    app = create_app()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
async def sec_client(security_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=security_app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def roles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(security.ROLE_PERMISSIONS, "technician", frozenset({"doors.*"}))
    monkeypatch.setitem(security.ROLE_PERMISSIONS, "viewer", frozenset({"doors.read", "b.y"}))


def test_password_hashing() -> None:
    hashed = hash_password("demo1234")
    assert hashed.startswith("$argon2id$")
    assert verify_password("demo1234", hashed)
    assert not verify_password("wrong", hashed)


def test_token_roundtrip_and_expiry() -> None:
    token = create_access_token(42, roles=["viewer", "viewer"], email="emma.employee@example.com")
    claims = decode_access_token(token)
    assert claims["sub"] == "42"
    assert claims["roles"] == ["viewer"]
    assert claims["email"] == "emma.employee@example.com"

    expired = create_access_token(42, ttl_minutes=-1)
    with pytest.raises(AuthenticationError, match="expired"):
        decode_access_token(expired)
    with pytest.raises(AuthenticationError, match="invalid"):
        decode_access_token(token[:-3] + "abc")


def test_permission_matching() -> None:
    assert permission_matches("*", "anything.at.all")
    assert permission_matches("doors.*", "doors.break")
    assert not permission_matches("doors.*", "doorsx.break")
    assert permission_matches("doors.read", "doors.read")
    assert not permission_matches("doors.read", "doors.break")


async def test_missing_or_bad_token_is_401(sec_client: AsyncClient) -> None:
    response = await sec_client.get("/api/v1/_test/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"] == "application/problem+json"

    bad = await sec_client.get("/api/v1/_test/me", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


@pytest.mark.usefixtures("roles")
async def test_require_permission(sec_client: AsyncClient, make_token: Callable[..., str]) -> None:
    def headers(*roles: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {make_token('u-1', roles=roles)}"}

    me = await sec_client.get("/api/v1/_test/me", headers=headers("viewer"))
    assert me.json() == {"id": "u-1", "roles": ["viewer"]}

    denied = await sec_client.post("/api/v1/_test/break", headers=headers("viewer"))
    assert denied.status_code == 403
    assert denied.json()["requiredPermissions"] == ["doors.break"]

    allowed = await sec_client.post("/api/v1/_test/break", headers=headers("technician"))
    assert allowed.json() == {"by": "u-1"}

    admin = await sec_client.post("/api/v1/_test/break", headers=headers("admin"))
    assert admin.status_code == 200

    either = await sec_client.post("/api/v1/_test/either", headers=headers("viewer"))
    assert either.status_code == 200
    neither = await sec_client.post("/api/v1/_test/either", headers=headers("technician"))
    assert neither.status_code == 403
