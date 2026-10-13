"""Generic workflow action endpoints (API.md §1.5, §3.8) — core dispatch to the ActionRegistry.

``POST /<resource>/{id}/actions/{action}`` → ``200`` detail; ``POST /<resource>/bulk/{action}``
→ ``200 BulkResult`` (each id in its own transaction). Missing required comments are
``422`` (SPEC_RESOLUTIONS #34).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actions import RESOURCES, ActionBody, ActionDef, BulkBody, actions
from app.core.auth import CurrentUser, can
from app.core.db import SessionDep, commit
from app.core.enums import Trigger
from app.core.errors import (
    ApiError,
    ForbiddenError,
    NotFoundError,
    ValidationProblem,
    problem_responses,
    validation_errors_to_dict,
)
from app.core.models import User
from app.core.read_models.assets import AssetDetail, build_asset_detail
from app.core.read_models.lease_returns import LeaseReturnDetail, build_lease_detail
from app.core.read_models.service_requests import ServiceRequestDetail, build_sr_detail
from app.core.schemas import BulkFailure, BulkResult
from app.core.transitions import check_version, load_for_update, transition_service

router = APIRouter(tags=["actions"])

_ERRORS = problem_responses(401, 403, 404, 409, 422, 502)
type DetailBuilder = Callable[[AsyncSession, Any, User | None], Awaitable[Any]]
_DETAIL: dict[str, DetailBuilder] = {
    "service_requests": build_sr_detail,
    "lease_returns": build_lease_detail,
    "assets": build_asset_detail,
}


def _definition(resource: str, action: str) -> ActionDef:
    definition = actions.get(resource, action)
    if definition is None or not definition.via_endpoint:
        raise NotFoundError(f"Unknown action '{action}'.", code="unknown_action")
    return definition


def _check_permission(resource: str, definition: ActionDef, user: User) -> None:
    config = RESOURCES[resource]
    for permission in (config.endpoint_permission, definition.permission):
        if permission is not None and not can(user, permission):
            keys = [permission] if isinstance(permission, str) else list(permission)
            raise ForbiddenError(
                "You do not have permission to perform this action.", requiredPermissions=keys
            )


def _parse(definition: ActionDef, raw: dict[str, Any]) -> ActionBody:
    try:
        payload = definition.payload_model.model_validate(raw)
    except ValidationError as exc:
        raise ValidationProblem(validation_errors_to_dict(exc.errors())) from exc
    needs_comment = (
        definition.requires_comment and "reason" not in definition.payload_model.model_fields
    )
    if needs_comment and not (payload.comment or "").strip():
        raise ValidationProblem({"comment": ["A comment is required for this action."]})
    return payload


async def _execute(
    session: AsyncSession,
    resource: str,
    definition: ActionDef,
    entity_id: int,
    user: User,
    payload: ActionBody,
) -> Any:
    entity = await load_for_update(session, RESOURCES[resource].model, entity_id)
    check_version(entity, payload.version)
    if definition.handler is not None:
        await definition.handler(session, entity, user, payload)
    else:
        await transition_service.apply(
            session,
            entity,
            definition.name,
            actor=user,
            trigger=Trigger.USER,
            payload=payload,
            comment=payload.comment,
        )
    return entity


async def dispatch(
    session: AsyncSession,
    resource: str,
    entity_id: int,
    action: str,
    user: User,
    raw: dict[str, Any],
) -> Any:
    definition = _definition(resource, action)
    _check_permission(resource, definition, user)
    payload = _parse(definition, raw)
    entity = await _execute(session, resource, definition, entity_id, user, payload)
    await commit(session)
    await session.refresh(entity)
    return await _DETAIL[resource](session, entity, user)


async def dispatch_bulk(
    session: AsyncSession, resource: str, action: str, user: User, raw: dict[str, Any]
) -> BulkResult:
    definition = _definition(resource, action)
    if not definition.bulk:
        raise NotFoundError(f"Action '{action}' is not available in bulk.", code="unknown_action")
    _check_permission(resource, definition, user)
    try:
        wrapper = BulkBody.model_validate(raw)
    except ValidationError as exc:
        raise ValidationProblem(validation_errors_to_dict(exc.errors())) from exc
    payload = _parse(definition, {k: v for k, v in raw.items() if k != "ids"})
    result = BulkResult(action=action)
    for entity_id in dict.fromkeys(wrapper.ids):
        try:
            await _execute(
                session,
                resource,
                definition,
                entity_id,
                user,
                payload.model_copy(update={"version": None}),
            )
            await commit(session)
            result.succeeded.append(entity_id)
        except ApiError as exc:
            await session.rollback()
            await session.refresh(user)  # rollback expired every loaded object
            result.failed.append(
                BulkFailure(id=entity_id, code=exc.code, message=exc.detail or exc.resolved_title)
            )
    return result


_ActionBodyParam = Annotated[
    dict[str, Any],
    Body(default_factory=dict, description="Action-specific fields + comment/version"),
]
_BulkBodyParam = Annotated[
    dict[str, Any], Body(description="{ ids: number[] (1–200), comment?, ...actionFields }")
]


@router.post(
    "/service-requests/{entity_id}/actions/{action}",
    response_model=ServiceRequestDetail,
    summary="Run a service request action",
    responses=_ERRORS,
)
async def sr_action(
    entity_id: int, action: str, session: SessionDep, user: CurrentUser, body: _ActionBodyParam
) -> ServiceRequestDetail:
    return await dispatch(session, "service_requests", entity_id, action, user, body)


@router.post(
    "/service-requests/bulk/{action}",
    response_model=BulkResult,
    summary="Run a service request action in bulk",
    responses=_ERRORS,
)
async def sr_bulk(
    action: str, session: SessionDep, user: CurrentUser, body: _BulkBodyParam
) -> BulkResult:
    return await dispatch_bulk(session, "service_requests", action, user, body)


@router.post(
    "/lease-returns/{entity_id}/actions/{action}",
    response_model=LeaseReturnDetail,
    summary="Run a lease return action",
    responses=_ERRORS,
)
async def lease_action(
    entity_id: int, action: str, session: SessionDep, user: CurrentUser, body: _ActionBodyParam
) -> LeaseReturnDetail:
    return await dispatch(session, "lease_returns", entity_id, action, user, body)


@router.post(
    "/lease-returns/bulk/{action}",
    response_model=BulkResult,
    summary="Run a lease return action in bulk",
    responses=_ERRORS,
)
async def lease_bulk(
    action: str, session: SessionDep, user: CurrentUser, body: _BulkBodyParam
) -> BulkResult:
    return await dispatch_bulk(session, "lease_returns", action, user, body)


@router.post(
    "/assets/{entity_id}/actions/{action}",
    response_model=AssetDetail,
    summary="Run an asset action",
    responses=_ERRORS,
)
async def asset_action(
    entity_id: int, action: str, session: SessionDep, user: CurrentUser, body: _ActionBodyParam
) -> AssetDetail:
    return await dispatch(session, "assets", entity_id, action, user, body)


@router.post(
    "/assets/bulk/{action}",
    response_model=BulkResult,
    summary="Run an asset action in bulk",
    responses=_ERRORS,
)
async def asset_bulk(
    action: str, session: SessionDep, user: CurrentUser, body: _BulkBodyParam
) -> BulkResult:
    return await dispatch_bulk(session, "assets", action, user, body)
