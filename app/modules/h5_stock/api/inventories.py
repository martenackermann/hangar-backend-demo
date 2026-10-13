"""Inventory endpoints (API.md §8.5)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Query, Response, status
from sqlalchemy import or_, select

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.enums import InventoryScanResult, InventoryStatus
from app.core.errors import NotFoundError, problem_responses
from app.core.files import signed_url
from app.core.models import StoredFile, User
from app.core.pagination import (
    OrderBy,
    Page,
    PageParamsDep,
    csv_enum,
    csv_ints,
    escape_like,
    fetch_page,
    sort_param,
)
from app.core.schemas import DownloadLink, ScanResult
from app.core.transitions import check_version

from ..models import Inventory, InventoryScan
from ..schemas import (
    InventoryCancelBody,
    InventoryCloseBody,
    InventoryCreate,
    InventoryDetail,
    InventoryScanBody,
    InventoryScanOut,
    InventorySummary,
    Reconciliation,
    ReconciliationCategory,
)
from ..services import inventory as service

router = APIRouter(prefix="/inventories", tags=["inventories"])

SORT = sort_param({"openedAt": Inventory.opened_at}, default="-openedAt", tiebreaker=Inventory.id)
SCAN_SORT = sort_param(
    {"scannedAt": InventoryScan.scanned_at}, default="-scannedAt", tiebreaker=InventoryScan.id
)
ERRORS = problem_responses(401, 403, 404, 409, 422)


@router.get(
    "",
    response_model=Page[InventorySummary],
    summary="List inventories",
    responses=problem_responses(401, 403, 422),
)
async def list_inventories(
    session: SessionDep,
    _user: Annotated[User, Depends(require("inventory.read"))],
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
    site_id: Annotated[list[int | str] | None, Depends(csv_ints("siteId"))] = None,
    statuses: Annotated[
        list[InventoryStatus] | None, Depends(csv_enum("status", InventoryStatus))
    ] = None,
) -> Page[InventorySummary]:
    stmt = select(Inventory).order_by(*order)
    if site_id:
        stmt = stmt.where(Inventory.site_id.in_([i for i in site_id if isinstance(i, int)]))
    if statuses:
        stmt = stmt.where(Inventory.status.in_(statuses))
    rows, meta = await fetch_page(session, stmt, paging)
    return Page[InventorySummary](
        data=[await service.build_summary(session, r) for r in rows], meta=meta
    )


@router.post(
    "",
    response_model=InventoryDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Open an inventory for a hub or the Campus",
    responses=ERRORS,
)
async def open_inventory(
    body: InventoryCreate,
    session: SessionDep,
    user: Annotated[User, Depends(require("inventory.manage"))],
) -> InventoryDetail:
    inventory = await service.open_inventory(session, body.site_id, body.note, user)
    await commit(session)
    await session.refresh(inventory)
    return await service.build_detail(session, inventory, user)


@router.get(
    "/{inventory_id}", response_model=InventoryDetail, summary="Inventory detail", responses=ERRORS
)
async def get_inventory(
    inventory_id: int,
    session: SessionDep,
    user: Annotated[User, Depends(require("inventory.read"))],
) -> InventoryDetail:
    return await service.build_detail(
        session, await service.get_inventory(session, inventory_id), user
    )


@router.post(
    "/{inventory_id}/scans",
    response_model=ScanResult[InventoryScanOut],
    summary="Scan a serial into an open inventory",
    responses=ERRORS,
)
async def scan(
    inventory_id: int,
    body: InventoryScanBody,
    session: SessionDep,
    user: Annotated[User, Depends(require("inventory.scan"))],
) -> ScanResult[InventoryScanOut]:
    inventory = await service.get_inventory(session, inventory_id, lock=True)
    result = await service.scan(session, inventory, body.serial, body.stock_location_id, user)
    await commit(session)
    return result


@router.get(
    "/{inventory_id}/scans",
    response_model=Page[InventoryScanOut],
    summary="Scans of an inventory",
    responses=ERRORS,
)
async def list_scans(
    inventory_id: int,
    session: SessionDep,
    _user: Annotated[User, Depends(require("inventory.read"))],
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SCAN_SORT)],
    results: Annotated[
        list[InventoryScanResult] | None, Depends(csv_enum("result", InventoryScanResult))
    ] = None,
    scanned_by_id: Annotated[int | None, Query(alias="scannedById")] = None,
    q: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[InventoryScanOut]:
    await service.get_inventory(session, inventory_id)
    stmt = select(InventoryScan).where(InventoryScan.inventory_id == inventory_id).order_by(*order)
    if results:
        stmt = stmt.where(InventoryScan.result.in_(results))
    if scanned_by_id is not None:
        stmt = stmt.where(InventoryScan.scanned_by == scanned_by_id)
    if q and q.strip():
        pattern = f"%{escape_like(q.strip().upper())}%"
        stmt = stmt.where(or_(InventoryScan.serial_number.ilike(pattern, escape="\\")))
    rows, meta = await fetch_page(session, stmt, paging)
    return Page[InventoryScanOut](data=[service.build_scan(r) for r in rows], meta=meta)


@router.delete(
    "/{inventory_id}/scans/{scan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Undo a scan (own scans, or inventory.manage)",
    responses=ERRORS,
    response_class=Response,
)
async def undo_scan(
    inventory_id: int,
    scan_id: int,
    session: SessionDep,
    user: Annotated[User, Depends(require("inventory.scan"))],
) -> Response:
    inventory = await service.get_inventory(session, inventory_id, lock=True)
    await service.undo_scan(session, inventory, scan_id, user)
    await commit(session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{inventory_id}/reconciliation",
    response_model=Reconciliation,
    summary="Reconciliation: missing / wrong location / unknown / matched",
    responses=ERRORS,
)
async def get_reconciliation(
    inventory_id: int,
    session: SessionDep,
    _user: Annotated[User, Depends(require("inventory.read"))],
    paging: PageParamsDep,
    category: Annotated[ReconciliationCategory | None, Query()] = None,
) -> Reconciliation:
    inventory = await service.get_inventory(session, inventory_id)
    return await service.reconciliation(session, inventory, category, paging)


@router.post(
    "/{inventory_id}/actions/close",
    response_model=InventoryDetail,
    summary="Close the inventory (optionally apply corrections)",
    responses=ERRORS,
)
async def close_inventory(
    inventory_id: int,
    body: InventoryCloseBody,
    session: SessionDep,
    user: Annotated[User, Depends(require("inventory.manage"))],
) -> InventoryDetail:
    inventory = await service.get_inventory(session, inventory_id, lock=True)
    check_version(inventory, body.version)
    await service.close_inventory(session, inventory, body.count_date, body.apply_corrections, user)
    await commit(session)
    await session.refresh(inventory)
    return await service.build_detail(session, inventory, user)


@router.post(
    "/{inventory_id}/actions/cancel",
    response_model=InventoryDetail,
    summary="Cancel the inventory (scans are kept)",
    responses=ERRORS,
)
async def cancel_inventory(
    inventory_id: int,
    session: SessionDep,
    user: Annotated[User, Depends(require("inventory.manage"))],
    body: Annotated[InventoryCancelBody | None, Body()] = None,
) -> InventoryDetail:
    inventory = await service.get_inventory(session, inventory_id, lock=True)
    check_version(inventory, body.version if body else None)
    await service.cancel_inventory(session, inventory, body.comment if body else None, user)
    await commit(session)
    await session.refresh(inventory)
    return await service.build_detail(session, inventory, user)


@router.get(
    "/{inventory_id}/reports/{kind}",
    response_model=DownloadLink,
    summary="Download a closed inventory's report (CSV)",
    responses=ERRORS,
)
async def get_report(
    inventory_id: int,
    kind: Literal["missing", "wrong_location", "unknown"],
    session: SessionDep,
    _user: Annotated[User, Depends(require("inventory.read"))],
) -> DownloadLink:
    inventory = await service.get_inventory(session, inventory_id)
    file_id = {
        "missing": inventory.missing_report_file_id,
        "wrong_location": inventory.wrong_location_report_file_id,
        "unknown": inventory.unknown_report_file_id,
    }[kind]
    stored = await session.get(StoredFile, file_id) if file_id is not None else None
    if inventory.status != InventoryStatus.CLOSED or stored is None:
        raise NotFoundError("Reports exist only for closed inventories.")
    return signed_url(stored)
