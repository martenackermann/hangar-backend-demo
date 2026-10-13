"""Installation places, personalization, software checklist and order closing (API.md §6.1–6.3)."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.enums import ItemSlot, ProcurementType, ServiceRequestStatus
from app.core.errors import ValidationProblem, problem_responses
from app.core.models import ServiceRequest, User
from app.core.pagination import (
    OrderBy,
    Page,
    PageParamsDep,
    csv_enum,
    csv_ints,
    fetch_page,
    sort_param,
)
from app.core.read_models import ServiceRequestDetail, ServiceRequestItemOut, build_sr_detail
from app.core.schemas import AssetSummary, ListResponse, ScanResult

from ..schemas import (
    AssetCandidates,
    AssignmentIn,
    InstallationPlaceOccupancy,
    InstallationPlaceScanIn,
    InstallationPlaceUndoIn,
    OrderClosingQueueItem,
    PersonalizationQueueItem,
    SoftwareChecklistIn,
    SoftwareChecklistOut,
    StagingPatch,
)
from ..services import fulfilment, personalization, scan
from ..services.common import get_hub, get_sr

router = APIRouter(tags=["staging"])

Scanner = Annotated[User, Depends(require("staging.scan"))]
Reader = Annotated[User, Depends(require("sr.read"))]
Personalizer = Annotated[User, Depends(require("staging.personalize"))]
ChecklistWriter = Annotated[
    User, Depends(require("staging.personalize", "staging.close", any_of=True))
]

QUEUE_SORT = sort_param(
    {
        "appointmentStartAt": ServiceRequest.appointment_start_at,
        "number": ServiceRequest.number,
        "claimedAt": ServiceRequest.claimed_at,
    },
    default="appointmentStartAt",
    tiebreaker=ServiceRequest.id,
)
CLOSING_SORT = sort_param(
    {
        "appointmentStartAt": ServiceRequest.appointment_start_at,
        "number": ServiceRequest.number,
    },
    default="appointmentStartAt",
    tiebreaker=ServiceRequest.id,
)
SiteParam = Annotated[int, Query(alias="siteId", description="Supplying hub (required).")]
RegionIds = Annotated[list[int | str] | None, Depends(csv_ints("regionId", allow=("none",)))]


def editor_param(
    value: Annotated[str | None, Query(alias="editorId", description="me | none | user id")] = None,
) -> int | str | None:
    if value is None or value == "":
        return None
    if value in ("me", "none"):
        return value
    if value.isdigit():
        return int(value)
    raise ValidationProblem({"editorId": ["Use 'me', 'none' or a user id."]})


# --- installation place scan station ------------------------------------------------------------


@router.post(
    "/scan/installation-place",
    response_model=ScanResult[AssetSummary],
    summary="Scan a serial onto an installation place",
    responses=problem_responses(401, 403, 404, 422),
)
async def scan_installation_place(
    body: InstallationPlaceScanIn, session: SessionDep, user: Scanner
) -> ScanResult[AssetSummary]:
    result = await scan.scan_installation_place(
        session, site_id=body.site_id, serial=body.serial, place=body.place, actor=user
    )
    await commit(session)
    return result


@router.post(
    "/scan/installation-place/undo",
    response_model=ScanResult[AssetSummary],
    summary="Undo an installation place scan",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def undo_installation_place(
    body: InstallationPlaceUndoIn, session: SessionDep, user: Scanner
) -> ScanResult[AssetSummary]:
    result = await scan.undo_installation_place(session, movement_id=body.movement_id, actor=user)
    await commit(session)
    return result


@router.get(
    "/staging/installation-places",
    response_model=ListResponse[InstallationPlaceOccupancy],
    summary="Occupied installation places",
    responses=problem_responses(401, 403, 404, 422),
)
async def list_installation_places(
    session: SessionDep,
    _user: Annotated[User, Depends(require("stock.read"))],
    site_id: Annotated[int | None, Query(alias="siteId")] = None,
    q: Annotated[str | None, Query(max_length=20, description="Place prefix")] = None,
) -> ListResponse[InstallationPlaceOccupancy]:
    return ListResponse[InstallationPlaceOccupancy](
        data=await scan.installation_places(session, site_id=site_id, q=q)
    )


# --- personalization ---------------------------------------------------------------------------


@router.get(
    "/personalization/queue",
    response_model=Page[PersonalizationQueueItem],
    summary="Personalization queue (appointment order)",
    responses=problem_responses(401, 403, 404, 422),
)
async def personalization_queue(
    session: SessionDep,
    user: Reader,
    paging: PageParamsDep,
    site_id: SiteParam,
    order: Annotated[list[OrderBy], Depends(QUEUE_SORT)],
    statuses: Annotated[
        list[ServiceRequestStatus] | None,
        Depends(
            csv_enum(
                "status",
                ServiceRequestStatus,
                description="appointment_booked, personalization_in_progress (default both)",
            )
        ),
    ],
    editor_id: Annotated[int | str | None, Depends(editor_param)],
    region_ids: RegionIds,
    procurement_types: Annotated[
        list[ProcurementType] | None, Depends(csv_enum("procurementType", ProcurementType))
    ],
    appointment_from: Annotated[date | None, Query(alias="appointmentFrom")] = None,
    appointment_to: Annotated[date | None, Query(alias="appointmentTo")] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[PersonalizationQueueItem]:
    await get_hub(session, site_id)
    filters = personalization.QueueFilters(
        site_id=site_id,
        statuses=statuses,
        editor_id=editor_id,
        region_ids=region_ids,
        procurement_types=procurement_types,
        appointment_from=appointment_from,
        appointment_to=appointment_to,
        q=q,
    )
    stmt = personalization.queue_stmt(filters, user).order_by(*order)
    rows, meta = await fetch_page(session, stmt, paging)
    return Page[PersonalizationQueueItem](
        data=await personalization.build_queue_items(session, rows, user), meta=meta
    )


@router.get(
    "/service-requests/{sr_id}/asset-candidates",
    response_model=AssetCandidates,
    summary="FIFO asset candidates for a new item",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def asset_candidates(
    sr_id: int,
    session: SessionDep,
    _user: Reader,
    slot: Annotated[ItemSlot, Query()],
) -> AssetCandidates:
    sr = await get_sr(session, sr_id)
    return await personalization.asset_candidates(session, sr, slot)


@router.post(
    "/service-requests/{sr_id}/items/{slot}/assignment",
    response_model=ScanResult[ServiceRequestItemOut],
    summary="Assign a stocked device to a new item by scanning its serial",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def assign_item(
    sr_id: int, slot: ItemSlot, body: AssignmentIn, session: SessionDep, user: Personalizer
) -> ScanResult[ServiceRequestItemOut]:
    sr = await get_sr(session, sr_id, for_update=True)
    result = await personalization.assign_by_scan(session, sr, slot, body.serial, user)
    await commit(session)
    return result


@router.delete(
    "/service-requests/{sr_id}/items/{slot}/assignment",
    response_model=ServiceRequestItemOut,
    summary="Unassign the device of a new item",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def unassign_item(
    sr_id: int, slot: ItemSlot, session: SessionDep, user: Personalizer
) -> ServiceRequestItemOut:
    sr = await get_sr(session, sr_id, for_update=True)
    result = await personalization.unassign(session, sr, slot, user)
    await commit(session)
    return result


@router.patch(
    "/service-requests/{sr_id}/staging",
    response_model=ServiceRequestDetail,
    summary="Staging options (OS reinstall, added software)",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def patch_staging(
    sr_id: int, body: StagingPatch, session: SessionDep, user: Personalizer
) -> ServiceRequestDetail:
    sr = await get_sr(session, sr_id, for_update=True)
    await personalization.patch_staging(session, sr, body, user)
    await commit(session)
    return await _detail(session, sr, user)


@router.get(
    "/service-requests/{sr_id}/software-checklist",
    response_model=SoftwareChecklistOut,
    summary="Software checklist",
    responses=problem_responses(401, 403, 404),
)
async def get_software_checklist(
    sr_id: int, session: SessionDep, _user: Reader
) -> SoftwareChecklistOut:
    return fulfilment.checklist_out(await get_sr(session, sr_id))


@router.put(
    "/service-requests/{sr_id}/software-checklist",
    response_model=SoftwareChecklistOut,
    summary="Save the software checklist",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def put_software_checklist(
    sr_id: int, body: SoftwareChecklistIn, session: SessionDep, _user: ChecklistWriter
) -> SoftwareChecklistOut:
    sr = await get_sr(session, sr_id, for_update=True)
    await personalization.set_software_checklist(
        session, sr, installed=body.installed_software, added=body.added_software
    )
    await commit(session)
    return fulfilment.checklist_out(sr)


# --- order closing -------------------------------------------------------------------------------


@router.get(
    "/order-closing/queue",
    response_model=Page[OrderClosingQueueItem],
    summary="Orders waiting for closing",
    responses=problem_responses(401, 403, 404, 422),
)
async def order_closing_queue(
    session: SessionDep,
    user: Reader,
    paging: PageParamsDep,
    site_id: SiteParam,
    order: Annotated[list[OrderBy], Depends(CLOSING_SORT)],
    region_ids: RegionIds,
    appointment_from: Annotated[date | None, Query(alias="appointmentFrom")] = None,
    appointment_to: Annotated[date | None, Query(alias="appointmentTo")] = None,
    checklist_complete: Annotated[bool | None, Query(alias="checklistComplete")] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[OrderClosingQueueItem]:
    await get_hub(session, site_id)
    stmt = fulfilment.order_closing_stmt(
        site_id=site_id,
        region_ids=region_ids,
        appointment_from=appointment_from,
        appointment_to=appointment_to,
        checklist_complete=checklist_complete,
        q=q,
    ).order_by(*order)
    rows, meta = await fetch_page(session, stmt, paging)
    return Page[OrderClosingQueueItem](
        data=await fulfilment.build_order_closing_items(session, rows, user), meta=meta
    )


async def _detail(session: AsyncSession, sr: ServiceRequest, user: User) -> ServiceRequestDetail:
    await session.refresh(sr)
    return await build_sr_detail(session, sr, user)
