"""Problem+json rendering for validation, HTTP, domain and unexpected errors."""

from collections.abc import AsyncIterator
from typing import Annotated

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import Field

from app.core.errors import (
    PROBLEM_JSON,
    ConflictError,
    DomainValidationError,
    NotFoundError,
    TransitionError,
)
from app.core.pagination import PageParamsDep
from app.core.schemas import ApiModel
from app.main import create_app


class ThingIn(ApiModel):
    serial_number: Annotated[str, Field(min_length=3)]
    quantity: int
    note: str | None = None


router = APIRouter(prefix="/_test", tags=["test"])


@router.post("/things", status_code=201)
async def create_thing(body: ThingIn) -> ThingIn:
    return body


@router.get("/things")
async def list_things(paging: PageParamsDep) -> dict[str, int]:
    return {"page": paging.page, "perPage": paging.per_page}


@router.get("/missing")
async def missing() -> None:
    raise NotFoundError("Thing 42 not found.")


@router.get("/conflict")
async def conflict() -> None:
    raise ConflictError("Serial already assigned.", serialNumber="SN-1")


@router.get("/domain-validation")
async def domain_validation() -> None:
    raise DomainValidationError({"serialNumber": "Unknown serial."})


@router.post("/transition")
async def transition() -> None:
    raise TransitionError(
        "Action 'ship' is not allowed while service_request is 'imported'.",
        action="ship",
        current_state="imported",
        allowed_actions=["invite", "cancel"],
    )


@router.get("/boom")
async def boom() -> None:
    raise RuntimeError("secret internals")


@pytest.fixture(scope="module")
def error_app() -> FastAPI:
    app = create_app()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
async def error_client(error_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=error_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_request_validation_is_problem_json_with_field_errors(
    error_client: AsyncClient,
) -> None:
    response = await error_client.post(
        "/api/v1/_test/things", json={"serialNumber": "x", "quantity": "many"}
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert body["type"] == "https://hangar.example.com/problems/validation-error"
    assert body["title"] == "Validation failed"
    assert body["status"] == 422
    assert body["instance"] == "/api/v1/_test/things"
    assert body["requestId"]
    assert set(body["errors"]) == {"serialNumber", "quantity"}
    assert body["errors"]["serialNumber"] == ["String should have at least 3 characters"]
    assert all(isinstance(m, str) for msgs in body["errors"].values() for m in msgs)


async def test_snake_case_names_are_accepted_and_camel_case_is_returned(
    error_client: AsyncClient,
) -> None:
    response = await error_client.post(
        "/api/v1/_test/things", json={"serial_number": "SN-100", "quantity": 2}
    )
    assert response.status_code == 201
    assert response.json() == {"serialNumber": "SN-100", "quantity": 2, "note": None}


async def test_query_validation_uses_param_alias(error_client: AsyncClient) -> None:
    response = await error_client.get("/api/v1/_test/things", params={"perPage": 500})
    assert response.status_code == 422
    assert list(response.json()["errors"]) == ["perPage"]

    ok = await error_client.get("/api/v1/_test/things", params={"page": 2, "perPage": 200})
    assert ok.json() == {"page": 2, "perPage": 200}


async def test_missing_body_reports_body_field(error_client: AsyncClient) -> None:
    response = await error_client.post("/api/v1/_test/things")
    assert response.status_code == 422
    assert "body" in response.json()["errors"]


@pytest.mark.parametrize(
    ("path", "status", "slug", "extra"),
    [
        ("/api/v1/_test/missing", 404, "not-found", {}),
        ("/api/v1/_test/conflict", 409, "conflict", {"serialNumber": "SN-1"}),
        (
            "/api/v1/_test/domain-validation",
            422,
            "validation-error",
            {"errors": {"serialNumber": ["Unknown serial."]}},
        ),
    ],
)
async def test_api_errors(
    error_client: AsyncClient, path: str, status: int, slug: str, extra: dict[str, object]
) -> None:
    response = await error_client.get(path)
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert body["type"].endswith(f"/problems/{slug}")
    assert body["status"] == status
    for key, value in extra.items():
        assert body[key] == value


async def test_transition_error_is_409(error_client: AsyncClient) -> None:
    response = await error_client.post("/api/v1/_test/transition")
    assert response.status_code == 409
    body = response.json()
    assert body["type"].endswith("/problems/transition-not-allowed")
    assert body["currentState"] == "imported"
    assert body["action"] == "ship"
    assert body["allowedActions"] == ["invite", "cancel"]


async def test_unknown_route_and_method(error_client: AsyncClient) -> None:
    not_found = await error_client.get("/api/v1/nope")
    assert not_found.status_code == 404
    assert not_found.headers["content-type"] == PROBLEM_JSON
    assert not_found.json()["type"] == "about:blank"
    assert not_found.json()["title"] == "Not Found"

    not_allowed = await error_client.delete("/api/v1/health")
    assert not_allowed.status_code == 405
    assert not_allowed.json()["title"] == "Method Not Allowed"


async def test_unexpected_error_does_not_leak(error_client: AsyncClient) -> None:
    response = await error_client.get("/api/v1/_test/boom")
    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_JSON
    assert "secret" not in response.text
