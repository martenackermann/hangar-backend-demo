"""Reference lists (API.md §3.4; ``active`` accepts true/false/all — SPEC_RESOLUTIONS #22)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, or_, select

from app.core.auth import CurrentUser, require
from app.core.db import SessionDep
from app.core.enums import LocationType, Role
from app.core.errors import problem_responses
from app.core.models import DeviationCode, Region, Site, StockLocation, User
from app.core.pagination import csv_enum, csv_ints, escape_like
from app.core.read_models.master import (
    DeviationCodeOut,
    RegionOut,
    SiteOut,
    StockLocationOut,
    UserSummary,
    build_deviation_code,
    build_regions,
    build_site,
    build_stock_location,
    build_user_summary,
)
from app.core.schemas import ListResponse

router = APIRouter(tags=["reference"])

type ActiveFilter = Literal["true", "false", "all"]
_AUTH = problem_responses(401, 403, 422)


def active_filter(stmt: Select, column: object, active: ActiveFilter) -> Select:
    if active == "all":
        return stmt
    return stmt.where(column.is_(active == "true"))  # type: ignore[attr-defined]


@router.get("/sites", response_model=ListResponse[SiteOut], summary="Sites", responses=_AUTH)
async def list_sites(session: SessionDep, _user: CurrentUser) -> ListResponse[SiteOut]:
    sites = (await session.scalars(select(Site).order_by(Site.sort_order, Site.id))).all()
    return ListResponse[SiteOut](data=[build_site(s) for s in sites])


@router.get(
    "/regions",
    response_model=ListResponse[RegionOut],
    summary="Regions",
    dependencies=[Depends(require("master.read"))],
    responses=_AUTH,
)
async def list_regions(
    session: SessionDep, active: Annotated[ActiveFilter, Query()] = "true"
) -> ListResponse[RegionOut]:
    stmt = active_filter(select(Region), Region.is_active, active).order_by(
        Region.sort_order, Region.name
    )
    regions = (await session.scalars(stmt)).all()
    return ListResponse[RegionOut](data=await build_regions(session, regions))


@router.get(
    "/stock-locations",
    response_model=ListResponse[StockLocationOut],
    summary="Stock locations",
    responses=_AUTH,
)
async def list_stock_locations(
    session: SessionDep,
    _user: CurrentUser,
    site_id: Annotated[list[int | str] | None, Depends(csv_ints("siteId"))] = None,
    types: Annotated[list[LocationType] | None, Depends(csv_enum("type", LocationType))] = None,
    active: Annotated[ActiveFilter, Query()] = "all",
) -> ListResponse[StockLocationOut]:
    stmt = select(StockLocation).order_by(
        StockLocation.site_id.nulls_last(), StockLocation.sort_order, StockLocation.id
    )
    if site_id:
        stmt = stmt.where(StockLocation.site_id.in_([i for i in site_id if isinstance(i, int)]))
    if types:
        stmt = stmt.where(StockLocation.type.in_(types))
    stmt = active_filter(stmt, StockLocation.is_active, active)
    return ListResponse[StockLocationOut](
        data=[build_stock_location(item_) for item_ in (await session.scalars(stmt)).all()]
    )


@router.get(
    "/users",
    response_model=ListResponse[UserSummary],
    summary="Users (names and roles)",
    dependencies=[Depends(require("users.read"))],
    responses=_AUTH,
)
async def list_users(
    session: SessionDep,
    roles: Annotated[list[Role] | None, Depends(csv_enum("role", Role))] = None,
    active: Annotated[ActiveFilter, Query()] = "true",
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> ListResponse[UserSummary]:
    stmt = active_filter(select(User), User.is_active, active).order_by(
        User.last_name, User.first_name, User.id
    )
    if roles:
        stmt = stmt.where(User.role.in_(roles))
    if q and q.strip():
        pattern = f"%{escape_like(q.strip())}%"
        name = User.first_name + " " + User.last_name
        stmt = stmt.where(
            or_(
                name.ilike(pattern, escape="\\"),
                func.lower(User.email).ilike(pattern.lower(), escape="\\"),
            )
        )
    return ListResponse[UserSummary](
        data=[build_user_summary(u) for u in (await session.scalars(stmt)).all()]
    )


@router.get(
    "/deviation-codes",
    response_model=ListResponse[DeviationCodeOut],
    summary="Deviation codes",
    responses=_AUTH,
)
async def list_deviation_codes(
    session: SessionDep, _user: CurrentUser, active: Annotated[ActiveFilter, Query()] = "true"
) -> ListResponse[DeviationCodeOut]:
    stmt = active_filter(select(DeviationCode), DeviationCode.is_active, active).order_by(
        DeviationCode.sort_order, DeviationCode.id
    )
    return ListResponse[DeviationCodeOut](
        data=[build_deviation_code(c) for c in (await session.scalars(stmt)).all()]
    )
