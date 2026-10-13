"""``/lease-returns`` list, detail, PATCH, comments, history, export (API.md §4.5)."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.errors import NotFoundError, problem_responses
from app.core.models import LeaseReturn, LeaseReturnComment, User
from app.core.pagination import OrderBy, Page, PageParamsDep, fetch_page, sort_param
from app.core.read_models import (
    LeaseReturnDetail,
    LeaseReturnListItem,
    build_lease_detail,
    build_lease_list_items,
    user_ref,
)
from app.core.schemas import DownloadLink
from app.core.transitions import load_for_update

from ..exports import LEASE_COLUMNS, export_filename, lease_rows, store_export, workbook
from ..history import lease_history
from ..queries import LEASE_SORT, LeaseFiltersDep, lease_select
from ..schemas import (
    CommentCreate,
    CommentList,
    HistoryList,
    LeaseReturnCommentOut,
    LeaseReturnPatch,
)
from ..services import add_comment, patch_lease

router = APIRouter(prefix="/lease-returns", tags=["lease-returns"])

SORT = sort_param(LEASE_SORT, default="retirementDate", tiebreaker=LeaseReturn.id)
Reader = Annotated[User, Depends(require("lease.read"))]
Manager = Annotated[User, Depends(require("lease.manage"))]


async def _get(session: SessionDep, lease_id: int) -> LeaseReturn:
    lease = await session.get(LeaseReturn, lease_id)
    if lease is None:
        raise NotFoundError(f"Lease return {lease_id} not found.")
    return lease


def _comment(comment: LeaseReturnComment) -> LeaseReturnCommentOut:
    author = user_ref(comment.author)
    assert author is not None
    return LeaseReturnCommentOut(
        id=comment.id, body=comment.body, author=author, created_at=comment.created_at
    )


@router.get(
    "",
    response_model=Page[LeaseReturnListItem],
    summary="List lease returns",
    responses=problem_responses(401, 403, 422),
)
async def list_lease_returns(
    session: SessionDep,
    user: Reader,
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
    filters: LeaseFiltersDep,
) -> Page[LeaseReturnListItem]:
    rows, meta = await fetch_page(session, lease_select(filters).order_by(*order), paging)
    return Page[LeaseReturnListItem](
        data=await build_lease_list_items(session, rows, user), meta=meta
    )


@router.post(
    "/export",
    response_model=DownloadLink,
    summary="Export lease returns (xlsx, list filters)",
    responses=problem_responses(401, 403, 422),
)
async def export_lease_returns(
    session: SessionDep,
    user: Annotated[User, Depends(require("lease.export"))],
    order: Annotated[list[OrderBy], Depends(SORT)],
    filters: LeaseFiltersDep,
) -> DownloadLink:
    rows = (await session.scalars(lease_select(filters).order_by(*order))).unique().all()
    data = workbook("Lease returns", LEASE_COLUMNS, lease_rows(rows))
    link = await store_export(session, data, export_filename("lease-returns"), user)
    await commit(session)
    return link


@router.get(
    "/{lease_id}",
    response_model=LeaseReturnDetail,
    summary="Lease return detail",
    responses=problem_responses(401, 403, 404),
)
async def get_lease_return(lease_id: int, session: SessionDep, user: Reader) -> LeaseReturnDetail:
    return await build_lease_detail(session, await _get(session, lease_id), user)


@router.patch(
    "/{lease_id}",
    response_model=LeaseReturnDetail,
    summary="Edit lease return fields",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def patch_lease_return(
    lease_id: int, body: LeaseReturnPatch, session: SessionDep, user: Manager
) -> LeaseReturnDetail:
    lease = await load_for_update(session, LeaseReturn, lease_id)
    await patch_lease(session, lease, body, user)
    await commit(session)
    await session.refresh(lease)
    return await build_lease_detail(session, lease, user)


@router.get(
    "/{lease_id}/comments",
    response_model=CommentList,
    summary="Lease return comments (newest first)",
    responses=problem_responses(401, 403, 404),
)
async def list_comments(lease_id: int, session: SessionDep, _user: Reader) -> CommentList:
    await _get(session, lease_id)
    rows = (
        await session.scalars(
            select(LeaseReturnComment)
            .where(LeaseReturnComment.lease_return_id == lease_id)
            .order_by(LeaseReturnComment.created_at.desc(), LeaseReturnComment.id.desc())
        )
    ).all()
    return CommentList(data=[_comment(c) for c in rows])


@router.post(
    "/{lease_id}/comments",
    response_model=LeaseReturnCommentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a lease return comment",
    responses=problem_responses(401, 403, 404, 422),
)
async def create_comment(
    lease_id: int, body: CommentCreate, session: SessionDep, user: Manager
) -> LeaseReturnCommentOut:
    lease = await _get(session, lease_id)
    comment = await add_comment(session, lease, body.body, user)
    await commit(session)
    return _comment(comment)


@router.get(
    "/{lease_id}/history",
    response_model=HistoryList,
    summary="Lease return timeline",
    responses=problem_responses(401, 403, 404),
)
async def lease_return_history(lease_id: int, session: SessionDep, _user: Reader) -> HistoryList:
    await _get(session, lease_id)
    return HistoryList(data=await lease_history(session, lease_id))
