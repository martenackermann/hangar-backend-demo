"""Saved views (API.md §3.6; decision D-22)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response
from pydantic import Field
from sqlalchemy import and_, or_, select

from app.core.auth import CurrentUser
from app.core.db import SessionDep, commit
from app.core.enums import Role
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, problem_responses
from app.core.models import SavedView, User
from app.core.read_models.refs import user_ref
from app.core.schemas import ApiInput, ApiModel, ListResponse, UserRef

router = APIRouter(prefix="/saved-views", tags=["saved-views"])
_SHARERS = {Role.DISPATCHER, Role.ADMIN}


class SavedViewCreate(ApiInput):
    route: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=80)
    search: dict[str, Any]
    is_shared: bool = False
    sort_order: int = Field(default=0, ge=0, le=32_000)


class SavedViewPatch(ApiInput):
    route: str | None = Field(default=None, min_length=1, max_length=120)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    search: dict[str, Any] | None = None
    is_shared: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=32_000)


class SavedViewOut(ApiModel):
    id: int
    route: str
    name: str
    search: dict[str, Any]
    is_shared: bool
    sort_order: int
    owner: UserRef
    is_own: bool
    created_at: datetime


def build_view(view: SavedView, user: User) -> SavedViewOut:
    owner = user_ref(view.user)
    assert owner is not None
    return SavedViewOut(
        id=view.id,
        route=view.route,
        name=view.name,
        search=view.search,
        is_shared=view.is_shared,
        sort_order=view.sort_order,
        owner=owner,
        is_own=view.user_id == user.id,
        created_at=view.created_at,
    )


def _check_share(user: User, shared: bool | None) -> None:
    if shared and user.role not in _SHARERS:
        raise ForbiddenError("Only dispatchers and admins can share views.")


async def _ensure_unique(
    session: SessionDep, user_id: int, route: str, name: str, view_id: int | None = None
) -> None:
    stmt = select(SavedView.id).where(
        SavedView.user_id == user_id, SavedView.route == route, SavedView.name == name
    )
    if view_id is not None:
        stmt = stmt.where(SavedView.id != view_id)
    if (await session.scalar(stmt)) is not None:
        raise ConflictError(
            "A view with this name already exists for this route.", code="duplicate"
        )


@router.get(
    "",
    response_model=ListResponse[SavedViewOut],
    summary="Own and shared views",
    responses=problem_responses(401),
)
async def list_views(
    session: SessionDep,
    user: CurrentUser,
    route: Annotated[str | None, Query(max_length=120)] = None,
) -> ListResponse[SavedViewOut]:
    stmt = select(SavedView).where(or_(SavedView.user_id == user.id, SavedView.is_shared))
    if route:
        stmt = stmt.where(SavedView.route == route)
    stmt = stmt.order_by(
        (SavedView.user_id != user.id), SavedView.sort_order, SavedView.name, SavedView.id
    )
    return ListResponse[SavedViewOut](
        data=[build_view(v, user) for v in (await session.scalars(stmt)).all()]
    )


@router.post(
    "",
    status_code=201,
    response_model=SavedViewOut,
    summary="Create a view",
    responses=problem_responses(401, 403, 409, 422),
)
async def create_view(
    body: SavedViewCreate, session: SessionDep, user: CurrentUser
) -> SavedViewOut:
    _check_share(user, body.is_shared)
    await _ensure_unique(session, user.id, body.route, body.name)
    view = SavedView(user_id=user.id, user=user, **body.model_dump(by_alias=False))
    session.add(view)
    await commit(session)
    return build_view(view, user)


async def _own(
    session: SessionDep, view_id: int, user: User, *, allow_admin_shared: bool = False
) -> SavedView:
    view = (await session.scalars(select(SavedView).where(and_(SavedView.id == view_id)))).first()
    if view is None or (view.user_id != user.id and not view.is_shared):
        raise NotFoundError("Saved view not found.")
    if view.user_id != user.id and not (allow_admin_shared and user.role == Role.ADMIN):
        raise ForbiddenError("Only the owner can change this view.")
    return view


@router.patch(
    "/{view_id}",
    response_model=SavedViewOut,
    summary="Update a view",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def update_view(
    view_id: int, body: SavedViewPatch, session: SessionDep, user: CurrentUser
) -> SavedViewOut:
    view = await _own(session, view_id, user)
    changes = body.model_dump(exclude_unset=True, by_alias=False)
    _check_share(user, changes.get("is_shared"))
    await _ensure_unique(
        session,
        view.user_id,
        changes.get("route") or view.route,
        changes.get("name") or view.name,
        view.id,
    )
    for key, value in changes.items():
        if value is not None:
            setattr(view, key, value)
    await commit(session)
    return build_view(view, user)


@router.delete(
    "/{view_id}",
    status_code=204,
    summary="Delete a view",
    responses=problem_responses(401, 403, 404),
)
async def delete_view(view_id: int, session: SessionDep, user: CurrentUser) -> Response:
    view = await _own(session, view_id, user, allow_admin_shared=True)
    await session.delete(view)
    await commit(session)
    return Response(status_code=204)
