"""Call-off order endpoints (API.md §8.2)."""

from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy import select

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.enums import CallOffOrderStatus
from app.core.errors import NotFoundError, problem_responses
from app.core.models import User
from app.core.pagination import (
    OrderBy,
    Page,
    PageParamsDep,
    csv_enum,
    csv_ints,
    fetch_page,
    sort_param,
)
from app.core.schemas import DownloadLink
from app.core.timeutil import business_datetime
from app.core.transitions import check_version

from ..models import CallOffOrder
from ..schemas import (
    CallOffCancelBody,
    CallOffOrderCreate,
    CallOffOrderOut,
    CallOffOrderPatch,
    CallOffOrderSummary,
    CallOffSendBody,
)
from ..services import calloff as service

router = APIRouter(prefix="/calloff-orders", tags=["calloff-orders"])

SORT = sort_param(
    {"createdAt": CallOffOrder.created_at, "deliveryDate": CallOffOrder.delivery_date},
    default="-createdAt",
    tiebreaker=CallOffOrder.id,
)
ERRORS = problem_responses(401, 403, 404, 409, 422)


async def load(session: SessionDep, order_id: int, *, lock: bool = False) -> CallOffOrder:
    stmt = select(CallOffOrder).where(CallOffOrder.id == order_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    order = (await session.scalars(stmt)).first()
    if order is None:
        raise NotFoundError(f"Call-off order {order_id} not found.")
    return order


@router.get(
    "",
    response_model=Page[CallOffOrderSummary],
    summary="List call-off orders",
    responses=problem_responses(401, 403, 422),
)
async def list_orders(
    session: SessionDep,
    _user: Annotated[User, Depends(require("stock.read"))],
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
    site_id: Annotated[list[int | str] | None, Depends(csv_ints("siteId"))] = None,
    statuses: Annotated[
        list[CallOffOrderStatus] | None, Depends(csv_enum("status", CallOffOrderStatus))
    ] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> Page[CallOffOrderSummary]:
    stmt = select(CallOffOrder).order_by(*order)
    if site_id:
        stmt = stmt.where(CallOffOrder.site_id.in_([i for i in site_id if isinstance(i, int)]))
    if statuses:
        stmt = stmt.where(CallOffOrder.status.in_(statuses))
    if date_from is not None:
        stmt = stmt.where(CallOffOrder.created_at >= business_datetime(date_from))
    if date_to is not None:
        stmt = stmt.where(CallOffOrder.created_at < business_datetime(date_to + timedelta(days=1)))
    rows, meta = await fetch_page(session, stmt, paging)
    return Page[CallOffOrderSummary](data=[service.build_summary(r) for r in rows], meta=meta)


@router.post(
    "",
    response_model=CallOffOrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft call-off order from the stock matrix",
    responses=ERRORS,
)
async def create_order(
    body: CallOffOrderCreate,
    session: SessionDep,
    user: Annotated[User, Depends(require("calloff.manage"))],
) -> CallOffOrderOut:
    order = await service.create_order(session, body, user)
    await commit(session)
    await session.refresh(order)
    return await service.build_order(session, order, user)


@router.get(
    "/{order_id}", response_model=CallOffOrderOut, summary="Call-off order detail", responses=ERRORS
)
async def get_order(
    order_id: int, session: SessionDep, user: Annotated[User, Depends(require("stock.read"))]
) -> CallOffOrderOut:
    return await service.build_order(session, await load(session, order_id), user)


@router.patch(
    "/{order_id}", response_model=CallOffOrderOut, summary="Edit a draft order", responses=ERRORS
)
async def patch_order(
    order_id: int,
    body: CallOffOrderPatch,
    session: SessionDep,
    user: Annotated[User, Depends(require("calloff.manage"))],
) -> CallOffOrderOut:
    order = await load(session, order_id, lock=True)
    check_version(order, body.version)
    await service.patch_order(session, order, body, user)
    await commit(session)
    await session.refresh(order)
    return await service.build_order(session, order, user)


@router.post(
    "/{order_id}/actions/send",
    response_model=CallOffOrderOut,
    summary="Send the order to Initech Logistics",
    responses=ERRORS,
)
async def send_order(
    order_id: int,
    session: SessionDep,
    user: Annotated[User, Depends(require("calloff.manage"))],
    body: Annotated[CallOffSendBody | None, Body()] = None,
) -> CallOffOrderOut:
    order = await load(session, order_id, lock=True)
    check_version(order, body.version if body else None)
    await service.send_order(session, order, user)
    await commit(session)
    await session.refresh(order)
    return await service.build_order(session, order, user)


@router.post(
    "/{order_id}/actions/cancel",
    response_model=CallOffOrderOut,
    summary="Cancel a draft order",
    responses=ERRORS,
)
async def cancel_order(
    order_id: int,
    session: SessionDep,
    user: Annotated[User, Depends(require("calloff.manage"))],
    body: Annotated[CallOffCancelBody | None, Body()] = None,
) -> CallOffOrderOut:
    order = await load(session, order_id, lock=True)
    check_version(order, body.version if body else None)
    await service.cancel_order(session, order, body.comment if body else None, user)
    await commit(session)
    await session.refresh(order)
    return await service.build_order(session, order, user)


@router.get(
    "/{order_id}/document",
    response_model=DownloadLink,
    summary="Order document (XLSX or PDF)",
    responses=ERRORS,
)
async def order_document(
    order_id: int,
    session: SessionDep,
    user: Annotated[User, Depends(require("stock.read"))],
    fmt: Annotated[Literal["xlsx", "pdf"], Query(alias="format")] = "pdf",
) -> DownloadLink:
    order = await load(session, order_id)
    link = await service.document_link(session, order, fmt, user)
    await commit(session)
    return link
