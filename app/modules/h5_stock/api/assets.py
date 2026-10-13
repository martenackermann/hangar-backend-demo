"""Asset endpoints (API.md §8.3). Status/location changes run through the core action
endpoints (``POST /assets/{id}/actions/{mark_missing|mark_found|correct}`` + bulk), whose
handlers this slice registers (SPEC_RESOLUTIONS #24)."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.enums import ArticleCategory, AssetSource, AssetStatus, LocationType
from app.core.errors import problem_responses
from app.core.models import Asset, AssetMovement, User
from app.core.pagination import (
    OrderBy,
    Page,
    PageParamsDep,
    csv_enum,
    csv_ints,
    fetch_page,
    sort_param,
)
from app.core.read_models.assets import (
    AssetDetail,
    AssetListItem,
    AssetMovementOut,
    build_asset_detail,
    build_asset_list_items,
    build_movement,
)
from app.core.schemas import DownloadLink
from app.core.transitions import check_version, load_for_update

from ..schemas import AssetPatch
from ..services import assets as service

router = APIRouter(prefix="/assets", tags=["assets"])

SORT = sort_param(
    {
        "serialNumber": Asset.serial_number,
        "assetTag": Asset.asset_tag,
        "status": Asset.status,
        "checkedInAt": Asset.checked_in_at,
        "statusChangedAt": Asset.status_changed_at,
    },
    default="-statusChangedAt",
    tiebreaker=Asset.id,
)
MOVEMENT_SORT = sort_param(
    {"occurredAt": AssetMovement.occurred_at},
    default="-occurredAt",
    tiebreaker=AssetMovement.id,
)
ERRORS = problem_responses(401, 403, 404, 409, 422)


def asset_filters(
    q: Annotated[str | None, Query(max_length=100)] = None,
    status: Annotated[list[AssetStatus] | None, Depends(csv_enum("status", AssetStatus))] = None,
    site_id: Annotated[list[int | str] | None, Depends(csv_ints("siteId"))] = None,
    location_id: Annotated[list[int | str] | None, Depends(csv_ints("locationId"))] = None,
    location_type: Annotated[
        list[LocationType] | None, Depends(csv_enum("locationType", LocationType))
    ] = None,
    article_id: Annotated[list[int | str] | None, Depends(csv_ints("articleId"))] = None,
    category: Annotated[
        list[ArticleCategory] | None, Depends(csv_enum("category", ArticleCategory))
    ] = None,
    service_request_id: Annotated[int | None, Query(alias="serviceRequestId")] = None,
    installation_place: Annotated[
        str | None, Query(alias="installationPlace", max_length=12)
    ] = None,
    has_installation_place: Annotated[bool | None, Query(alias="hasInstallationPlace")] = None,
    checked_in_from: Annotated[date | None, Query(alias="checkedInFrom")] = None,
    checked_in_to: Annotated[date | None, Query(alias="checkedInTo")] = None,
    source: Annotated[list[AssetSource] | None, Depends(csv_enum("source", AssetSource))] = None,
) -> service.AssetFilters:
    return service.AssetFilters(
        q=q,
        status=status,
        site_id=site_id,
        location_id=location_id,
        location_type=location_type,
        article_id=article_id,
        category=category,
        service_request_id=service_request_id,
        installation_place=installation_place,
        has_installation_place=has_installation_place,
        checked_in_from=checked_in_from,
        checked_in_to=checked_in_to,
        source=source,
    )


Filters = Annotated[service.AssetFilters, Depends(asset_filters)]


@router.get(
    "",
    response_model=Page[AssetListItem],
    summary="List assets",
    responses=problem_responses(401, 403, 422),
)
async def list_assets(
    session: SessionDep,
    user: Annotated[User, Depends(require("stock.read"))],
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
    filters: Filters,
) -> Page[AssetListItem]:
    rows, meta = await fetch_page(session, service.asset_query(filters).order_by(*order), paging)
    return Page[AssetListItem](data=await build_asset_list_items(session, rows, user), meta=meta)


@router.post(
    "/export",
    response_model=DownloadLink,
    summary="Export assets (CSV, list filters apply)",
    responses=problem_responses(401, 403, 422),
)
async def export_assets(
    session: SessionDep,
    user: Annotated[User, Depends(require("assets.export"))],
    order: Annotated[list[OrderBy], Depends(SORT)],
    filters: Filters,
) -> DownloadLink:
    link = await service.export_assets(session, filters, order, user)
    await commit(session)
    return link


@router.get(
    "/by-serial/{serial}",
    response_model=AssetDetail,
    summary="Asset by serial number (or asset tag)",
    responses=ERRORS,
)
async def asset_by_serial(
    serial: str, session: SessionDep, user: Annotated[User, Depends(require("stock.read"))]
) -> AssetDetail:
    return await build_asset_detail(session, await service.asset_by_serial(session, serial), user)


@router.get("/{asset_id}", response_model=AssetDetail, summary="Asset detail", responses=ERRORS)
async def get_asset(
    asset_id: int, session: SessionDep, user: Annotated[User, Depends(require("stock.read"))]
) -> AssetDetail:
    return await build_asset_detail(session, await service.get_asset(session, asset_id), user)


@router.get(
    "/{asset_id}/movements",
    response_model=Page[AssetMovementOut],
    summary="Movement ledger of an asset",
    responses=ERRORS,
)
async def list_movements(
    asset_id: int,
    session: SessionDep,
    _user: Annotated[User, Depends(require("stock.read"))],
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(MOVEMENT_SORT)],
) -> Page[AssetMovementOut]:
    await service.get_asset(session, asset_id)
    stmt = select(AssetMovement).where(AssetMovement.asset_id == asset_id).order_by(*order)
    rows, meta = await fetch_page(session, stmt, paging)
    return Page[AssetMovementOut](data=[build_movement(m) for m in rows], meta=meta)


@router.patch(
    "/{asset_id}",
    response_model=AssetDetail,
    summary="Correct asset metadata (tag, MAC, ITSM ids)",
    responses=ERRORS,
)
async def patch_asset(
    asset_id: int,
    body: AssetPatch,
    session: SessionDep,
    user: Annotated[User, Depends(require("assets.update"))],
) -> AssetDetail:
    asset = await load_for_update(session, Asset, asset_id)
    check_version(asset, body.version)
    await service.patch_asset(session, asset, body)
    await commit(session)
    await session.refresh(asset)
    return await build_asset_detail(session, asset, user)
