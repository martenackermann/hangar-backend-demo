"""Pagination envelope, sort parsing and search against a throwaway table."""

from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.db import SessionDep, get_session
from app.core.errors import DomainValidationError
from app.core.pagination import (
    OrderBy,
    Page,
    PageParams,
    PageParamsDep,
    paginate,
    parse_sort,
    search_clause,
    sort_param,
)
from app.core.schemas import ApiModel
from app.main import create_app


class _TestBase(DeclarativeBase):
    pass


class Widget(_TestBase):
    __tablename__ = "test_widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    serial_number: Mapped[str | None] = mapped_column(String(50))


class WidgetOut(ApiModel):
    id: int
    name: str
    serial_number: str | None


SORT = sort_param(
    {"name": Widget.name, "serialNumber": Widget.serial_number},
    default="name",
    tiebreaker=Widget.id,
)

router = APIRouter(prefix="/_test/widgets", tags=["test"])


@router.get("", response_model=Page[WidgetOut])
async def list_widgets(
    session: SessionDep,
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
    q: str | None = None,
) -> Page[WidgetOut]:
    stmt = select(Widget).order_by(*order)
    if (clause := search_clause(q, Widget.name, Widget.serial_number)) is not None:
        stmt = stmt.where(clause)
    return await paginate(session, stmt, paging, WidgetOut)


@pytest.fixture
async def widgets(db_session: AsyncSession) -> AsyncSession:
    await db_session.run_sync(lambda s: _TestBase.metadata.create_all(s.connection()))
    db_session.add_all(
        Widget(id=i, name=f"widget-{i:02d}", serial_number=None if i % 5 == 0 else f"SN_{i}")
        for i in range(1, 24)
    )
    await db_session.flush()
    return db_session


@pytest.fixture
async def widget_client(widgets: AsyncSession) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app()
    app.include_router(router, prefix="/api/v1")

    async def _session() -> AsyncIterator[AsyncSession]:
        yield widgets

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def test_parse_sort() -> None:
    assert parse_sort("name,-serialNumber", ["name", "serialNumber"]) == [
        ("name", False),
        ("serialNumber", True),
    ]
    assert parse_sort(None, ["name"]) == []
    assert parse_sort(" name , ", ["name"]) == [("name", False)]
    with pytest.raises(DomainValidationError) as caught:
        parse_sort("name,-secret,name", ["name"])
    assert caught.value.extensions["errors"] == {
        "sort": [
            "Unknown sort field 'secret'. Allowed: name.",
            "Sort field 'name' is given more than once.",
        ]
    }


def test_page_meta() -> None:
    assert PageParams(page=3, per_page=10).offset == 20
    meta = PageParams(page=1, per_page=10).meta(21)
    assert (meta.total, meta.total_pages) == (21, 3)
    assert PageParams().meta(0).total_pages == 0
    assert Page[WidgetOut].__name__ == "WidgetOutPage"


async def test_paginated_list_envelope(widget_client: AsyncClient) -> None:
    response = await widget_client.get("/api/v1/_test/widgets", params={"perPage": 10, "page": 3})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["meta"] == {"page": 3, "perPage": 10, "total": 23, "totalPages": 3}
    assert [w["name"] for w in body["data"]] == ["widget-21", "widget-22", "widget-23"]
    assert set(body["data"][0]) == {"id", "name", "serialNumber"}


async def test_sort_desc_with_nulls_last(widget_client: AsyncClient) -> None:
    response = await widget_client.get(
        "/api/v1/_test/widgets", params={"sort": "-serialNumber", "perPage": 200}
    )
    serials = [w["serialNumber"] for w in response.json()["data"]]
    assert serials[0] == "SN_9"
    assert serials[-4:] == [None] * 4


async def test_invalid_sort_is_422(widget_client: AsyncClient) -> None:
    response = await widget_client.get("/api/v1/_test/widgets", params={"sort": "password"})
    assert response.status_code == 422
    assert "sort" in response.json()["errors"]


async def test_search_escapes_like_wildcards(widget_client: AsyncClient) -> None:
    response = await widget_client.get("/api/v1/_test/widgets", params={"q": "sn_1"})
    names = [w["name"] for w in response.json()["data"]]
    # "_" is literal: matches SN_1, SN_11..SN_19 (SN_10 and SN_15 have no serial)
    assert names == [f"widget-{i:02d}" for i in (1, 11, 12, 13, 14, 16, 17, 18, 19)]

    everything = await widget_client.get("/api/v1/_test/widgets", params={"q": "%"})
    assert everything.json()["meta"]["total"] == 0
