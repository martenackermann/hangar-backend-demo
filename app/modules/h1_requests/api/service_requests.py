"""``/service-requests`` list, detail, PATCH, history, export (API.md §4.1–4.4)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.errors import NotFoundError, problem_responses
from app.core.models import ServiceRequest, User
from app.core.pagination import OrderBy, Page, PageParamsDep, fetch_page, sort_param
from app.core.read_models import (
    ServiceRequestDetail,
    ServiceRequestListItem,
    build_sr_detail,
    build_sr_list_items,
)
from app.core.schemas import DownloadLink
from app.core.transitions import load_for_update

from ..exports import SR_COLUMNS, export_filename, sr_rows, store_export, workbook
from ..history import sr_history
from ..queries import SR_SORT, SrFiltersDep, sr_select
from ..schemas import HistoryList, ServiceRequestPatch
from ..services import patch_service_request

router = APIRouter(prefix="/service-requests", tags=["service-requests"])

SORT = sort_param(SR_SORT, default="-importedAt", tiebreaker=ServiceRequest.id)
Reader = Annotated[User, Depends(require("sr.read"))]


async def _get(session: SessionDep, sr_id: int) -> ServiceRequest:
    sr = await session.get(ServiceRequest, sr_id, populate_existing=True)
    if sr is None:
        raise NotFoundError(f"Service request {sr_id} not found.")
    return sr


@router.get(
    "",
    response_model=Page[ServiceRequestListItem],
    summary="List service requests",
    responses=problem_responses(401, 403, 422),
)
async def list_service_requests(
    session: SessionDep,
    user: Reader,
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
    filters: SrFiltersDep,
) -> Page[ServiceRequestListItem]:
    rows, meta = await fetch_page(session, sr_select(filters, user).order_by(*order), paging)
    return Page[ServiceRequestListItem](
        data=await build_sr_list_items(session, rows, user), meta=meta
    )


@router.post(
    "/export",
    response_model=DownloadLink,
    summary="Export service requests (xlsx, list filters)",
    responses=problem_responses(401, 403, 422),
)
async def export_service_requests(
    session: SessionDep,
    user: Annotated[User, Depends(require("sr.export"))],
    order: Annotated[list[OrderBy], Depends(SORT)],
    filters: SrFiltersDep,
) -> DownloadLink:
    rows = (await session.scalars(sr_select(filters, user).order_by(*order))).unique().all()
    data = workbook("Service requests", SR_COLUMNS, sr_rows(rows))
    link = await store_export(session, data, export_filename("service-requests"), user)
    await commit(session)
    return link


@router.get(
    "/by-number/{number}",
    response_model=ServiceRequestDetail,
    summary="Service request by number",
    responses=problem_responses(401, 403, 404),
)
async def get_service_request_by_number(
    number: str, session: SessionDep, user: Reader
) -> ServiceRequestDetail:
    sr = (
        await session.scalars(
            select(ServiceRequest)
            .where(ServiceRequest.number == number.strip().upper())
            .execution_options(populate_existing=True)
        )
    ).first()
    if sr is None:
        raise NotFoundError(f"Service request {number} not found.")
    return await build_sr_detail(session, sr, user)


@router.get(
    "/{sr_id}",
    response_model=ServiceRequestDetail,
    summary="Service request detail",
    responses=problem_responses(401, 403, 404),
)
async def get_service_request(
    sr_id: int, session: SessionDep, user: Reader
) -> ServiceRequestDetail:
    return await build_sr_detail(session, await _get(session, sr_id), user)


@router.patch(
    "/{sr_id}",
    response_model=ServiceRequestDetail,
    summary="Edit service request fields",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def patch_service_request_endpoint(
    sr_id: int,
    body: ServiceRequestPatch,
    session: SessionDep,
    user: Annotated[User, Depends(require("sr.update"))],
) -> ServiceRequestDetail:
    sr = await load_for_update(session, ServiceRequest, sr_id)
    await patch_service_request(session, sr, body, user)
    await commit(session)
    await session.refresh(sr)
    return await build_sr_detail(session, sr, user)


@router.get(
    "/{sr_id}/history",
    response_model=HistoryList,
    summary="Service request timeline",
    responses=problem_responses(401, 403, 404),
)
async def service_request_history(sr_id: int, session: SessionDep, _user: Reader) -> HistoryList:
    await _get(session, sr_id)
    return HistoryList(data=await sr_history(session, sr_id))
