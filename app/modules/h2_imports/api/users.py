"""User administration ``/users`` writes (API.md §5.5). ``GET /users`` is core."""

from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Response

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.errors import problem_responses
from app.core.models import User
from app.core.read_models.master import UserSummary, build_user_summary

from ..schemas import EmptyBody, ResetPasswordIn, UserCreate, UserPatch
from ..services import users as service

router = APIRouter(prefix="/users", tags=["users"])

ManageUser = Annotated[User, Depends(require("users.manage"))]
_ERRORS = problem_responses(401, 403, 404, 409, 422)


@router.get(
    "/{user_id}",
    response_model=UserSummary,
    summary="User",
    responses=problem_responses(401, 403, 404),
)
async def get_user(user_id: int, session: SessionDep, _admin: ManageUser) -> UserSummary:
    return build_user_summary(await service.get_user(session, user_id))


@router.post(
    "",
    status_code=201,
    response_model=UserSummary,
    summary="Create a user",
    responses=_ERRORS,
)
async def create_user(body: UserCreate, session: SessionDep, _admin: ManageUser) -> UserSummary:
    user = await service.create_user(session, body)
    out = build_user_summary(user)
    await commit(session)
    return out


@router.patch(
    "/{user_id}",
    response_model=UserSummary,
    summary="Update names, role or home site",
    responses=_ERRORS,
)
async def update_user(
    user_id: int, body: UserPatch, session: SessionDep, admin: ManageUser
) -> UserSummary:
    user = await service.get_user(session, user_id)
    await service.update_user(session, user, body, admin)
    out = build_user_summary(user)
    await commit(session)
    return out


@router.post(
    "/{user_id}/actions/reset_password",
    status_code=204,
    response_class=Response,
    summary="Set a new password",
    responses=_ERRORS,
)
async def reset_password(
    user_id: int,
    session: SessionDep,
    _admin: ManageUser,
    body: Annotated[ResetPasswordIn, Body()],
) -> Response:
    user = await service.get_user(session, user_id)
    await service.reset_password(session, user, body.new_password)
    await commit(session)
    return Response(status_code=204)


@router.post(
    "/{user_id}/actions/{action}",
    response_model=UserSummary,
    summary="Activate or deactivate a user",
    responses=_ERRORS,
)
async def user_action(
    user_id: int,
    action: Literal["activate", "deactivate"],
    session: SessionDep,
    admin: ManageUser,
    _body: EmptyBody | None = None,
) -> UserSummary:
    user = await service.get_user(session, user_id)
    await service.set_active(session, user, action == "activate", admin)
    out = build_user_summary(user)
    await commit(session)
    return out
