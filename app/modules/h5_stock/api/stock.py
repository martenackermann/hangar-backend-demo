"""Stock matrix endpoints (API.md §8.1)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.enums import ArticleCategory, JobKind
from app.core.errors import problem_responses
from app.core.jobs import enqueue
from app.core.models import User
from app.core.pagination import csv_enum, csv_ints
from app.core.schemas import DownloadLink, JobAccepted

from ..schemas import BuffersPatch, StockCellList, StockMatrix
from ..services import stock as service
from ..services.common import RECALC_TASK

router = APIRouter(prefix="/stock", tags=["stock"])

SiteIds = Annotated[list[int | str] | None, Depends(csv_ints("siteId"))]
Categories = Annotated[list[ArticleCategory] | None, Depends(csv_enum("category", ArticleCategory))]


def matrix_filters(
    site_id: SiteIds = None,
    category: Categories = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    only_shortfall: Annotated[bool, Query(alias="onlyShortfall")] = False,
    include_inactive: Annotated[bool, Query(alias="includeInactive")] = False,
) -> service.MatrixFilters:
    return service.MatrixFilters(
        site_ids=site_id,
        categories=category,
        q=q,
        only_shortfall=only_shortfall,
        include_inactive=include_inactive,
    )


Filters = Annotated[service.MatrixFilters, Depends(matrix_filters)]


@router.get(
    "/matrix",
    response_model=StockMatrix,
    summary="Stock matrix (article × hub)",
    responses=problem_responses(401, 403, 422),
)
async def get_matrix(
    session: SessionDep,
    _user: Annotated[User, Depends(require("stock.read"))],
    filters: Filters,
) -> StockMatrix:
    return await service.build_matrix(session, filters)


@router.post(
    "/recalculate",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Recalculate stock and demand now",
    responses=problem_responses(401, 403),
)
async def recalculate_now(
    session: SessionDep, user: Annotated[User, Depends(require("stock.recalculate"))]
) -> JobAccepted:
    job = await enqueue(session, RECALC_TASK, kind=JobKind.STOCK_RECALCULATE, created_by=user)
    await commit(session)
    return JobAccepted(job_id=job.id)


@router.patch(
    "/buffers",
    response_model=StockCellList,
    summary="Edit buffers of one hub",
    responses=problem_responses(401, 403, 422),
)
async def patch_buffers(
    body: BuffersPatch,
    session: SessionDep,
    user: Annotated[User, Depends(require("stock.buffers"))],
) -> StockCellList:
    cells = await service.update_buffers(session, body, user)
    await commit(session)
    return StockCellList(data=cells)


@router.post(
    "/export",
    response_model=DownloadLink,
    summary="Export the stock matrix (CSV)",
    responses=problem_responses(401, 403, 422),
)
async def export_matrix(
    session: SessionDep,
    user: Annotated[User, Depends(require("assets.export"))],
    filters: Filters,
) -> DownloadLink:
    link = await service.export_matrix(session, filters, user)
    await commit(session)
    return link
