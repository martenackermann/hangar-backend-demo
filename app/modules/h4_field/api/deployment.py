"""Deployment endpoints (API.md §7.3): view, scan, verification undo, unknown devices,
protocol download / re-send."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.auth import CurrentUser, require
from app.core.db import SessionDep, commit
from app.core.enums import ItemSlot, ServiceRequestStatus
from app.core.errors import NotFoundError, problem_responses
from app.core.files import signed_url
from app.core.models import ServiceRequest, User
from app.core.read_models import ServiceRequestItemOut, build_item
from app.core.schemas import DownloadLink, ScanResult
from app.core.transitions import load_for_update

from ..deployment import (
    deployment_view,
    register_unknown,
    require_executor,
    scan,
    stop_context,
    undo_verification,
)
from ..protocol import ensure_protocol, send_protocol
from ..schemas import (
    DeploymentScanBody,
    DeploymentViewOut,
    ProtocolQueued,
    ProtocolSendBody,
    RegisterUnknownBody,
)

router = APIRouter(tags=["field"])

_ERRORS = problem_responses(401, 403, 404, 409, 422)


@router.get(
    "/tours/{tour_id}/stops/{stop_id}/deployment",
    response_model=DeploymentViewOut,
    summary="Deployment view of a tour stop",
    responses=_ERRORS,
)
async def get_deployment(
    tour_id: int, stop_id: int, session: SessionDep, user: CurrentUser
) -> DeploymentViewOut:
    return await deployment_view(session, tour_id, stop_id, user)


@router.post(
    "/scan/deployment",
    response_model=ScanResult[ServiceRequestItemOut],
    summary="Deployment scan (verify a new or old device)",
    responses=_ERRORS,
)
async def scan_deployment(
    body: DeploymentScanBody, session: SessionDep, user: CurrentUser
) -> ScanResult[ServiceRequestItemOut]:
    require_executor(user)
    sr = await load_for_update(session, ServiceRequest, body.service_request_id)
    result = await scan(session, sr, body.serial, user)
    await commit(session)
    return result


@router.delete(
    "/service-requests/{sr_id}/items/{slot}/verification",
    response_model=ServiceRequestItemOut,
    summary="Undo a deployment verification",
    responses=_ERRORS,
)
async def delete_verification(
    sr_id: int, slot: ItemSlot, session: SessionDep, user: CurrentUser
) -> ServiceRequestItemOut:
    require_executor(user)
    sr = await load_for_update(session, ServiceRequest, sr_id)
    item = await undo_verification(session, sr, slot, user)
    await commit(session)
    return build_item(item)


@router.post(
    "/service-requests/{sr_id}/items/{slot}/register-unknown",
    status_code=201,
    response_model=ServiceRequestItemOut,
    summary="Register an unknown old device found on site",
    responses=_ERRORS,
)
async def post_register_unknown(
    sr_id: int,
    slot: ItemSlot,
    body: RegisterUnknownBody,
    session: SessionDep,
    user: CurrentUser,
) -> ServiceRequestItemOut:
    require_executor(user)
    sr = await load_for_update(session, ServiceRequest, sr_id)
    item = await register_unknown(
        session,
        sr,
        slot,
        serial=body.serial,
        asset_tag=body.asset_tag,
        model=body.model,
        user=user,
    )
    await commit(session)
    return build_item(item)


async def _completed_sr(session: SessionDep, sr_id: int) -> ServiceRequest:
    sr = await session.get(ServiceRequest, sr_id)
    if sr is None or sr.status != ServiceRequestStatus.COMPLETED:
        raise NotFoundError("A protocol exists only for completed requests.")
    return sr


@router.get(
    "/service-requests/{sr_id}/protocol",
    response_model=DownloadLink,
    summary="Download link of the handover protocol",
    responses=problem_responses(401, 403, 404),
)
async def get_protocol(
    sr_id: int,
    session: SessionDep,
    user: Annotated[User, Depends(require("sr.read"))],
) -> DownloadLink:
    sr = await _completed_sr(session, sr_id)
    stored = await ensure_protocol(session, sr, created_by=user)
    await commit(session)  # persists a regenerated protocol (SPEC §6.11)
    return signed_url(stored)


@router.post(
    "/service-requests/{sr_id}/protocol/send",
    status_code=202,
    response_model=ProtocolQueued,
    summary="E-mail the handover protocol again",
    responses=_ERRORS,
)
async def post_protocol_send(
    sr_id: int,
    session: SessionDep,
    user: CurrentUser,
    body: ProtocolSendBody | None = None,
) -> ProtocolQueued:
    require_executor(user)
    sr = await _completed_sr(session, sr_id)
    await stop_context(session, sr, user, pending_only=False)
    notification_id = await send_protocol(
        session, sr, email=str(body.email) if body and body.email else None, actor=user
    )
    await commit(session)
    return ProtocolQueued(notification_id=notification_id)
