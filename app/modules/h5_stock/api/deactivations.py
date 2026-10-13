"""Deactivation upload endpoints (API.md §8.4 + SPEC_RESOLUTIONS #23)."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.enums import DeactivationReason
from app.core.errors import NotFoundError, problem_responses
from app.core.files import SPREADSHEETS, read_upload
from app.core.models import User
from app.core.pagination import OrderBy, Page, PageParamsDep, fetch_page, sort_param
from app.core.schemas import DownloadLink

from ..models import DeactivationBatch
from ..schemas import DeactivationBatchOut, DeactivationResult
from ..services import deactivation as service

router = APIRouter(prefix="/deactivations", tags=["deactivations"])

SORT = sort_param(
    {"createdAt": DeactivationBatch.created_at},
    default="-createdAt",
    tiebreaker=DeactivationBatch.id,
)


@router.post(
    "",
    response_model=DeactivationResult,
    summary="Deactivate devices from a serial list (CSV/XLSX)",
    responses=problem_responses(400, 401, 403, 413, 415, 422),
)
async def upload_deactivation(
    session: SessionDep,
    user: Annotated[User, Depends(require("assets.deactivate"))],
    file: Annotated[UploadFile, File(description="CSV or XLSX with a serial_number column")],
    reason: Annotated[DeactivationReason, Form()],
) -> DeactivationResult:
    upload = await read_upload(file, allowed=SPREADSHEETS)
    batch = await service.deactivate_upload(session, upload, reason, user)
    await commit(session)
    await session.refresh(batch)
    return await service.build_result(session, batch)


@router.get(
    "",
    response_model=Page[DeactivationBatchOut],
    summary="Deactivation history",
    responses=problem_responses(401, 403, 422),
)
async def list_batches(
    session: SessionDep,
    _user: Annotated[User, Depends(require("stock.read"))],
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
) -> Page[DeactivationBatchOut]:
    rows, meta = await fetch_page(session, select(DeactivationBatch).order_by(*order), paging)
    return Page[DeactivationBatchOut](data=[service.build_batch(r) for r in rows], meta=meta)


@router.get(
    "/sample",
    response_model=DownloadLink,
    summary="Sample upload file (CSV)",
    responses=problem_responses(401, 403),
)
async def sample(
    session: SessionDep, user: Annotated[User, Depends(require("assets.deactivate"))]
) -> DownloadLink:
    link = await service.sample_file(session, user)
    await commit(session)
    return link


@router.get(
    "/{batch_id}",
    response_model=DeactivationResult,
    summary="Result of one deactivation upload",
    responses=problem_responses(401, 403, 404),
)
async def get_batch(
    batch_id: int, session: SessionDep, _user: Annotated[User, Depends(require("stock.read"))]
) -> DeactivationResult:
    batch = await session.get(DeactivationBatch, batch_id)
    if batch is None:
        raise NotFoundError(f"Deactivation batch {batch_id} not found.")
    return await service.build_result(session, batch)
