"""ActionRegistry — generic workflow actions of SRs, lease returns and assets (SPEC §2.4, D-02).

Slices register actions; core dispatches ``POST /<resource>/{id}/actions/{action}`` and
``POST /<resource>/bulk/{action}`` to them and computes ``availableActions`` /
``blockedActions`` for every serializer:

    reg.register_action(resource="service_requests", name="claim",
                        permission="staging.personalize", payload_model=None, handler=None,
                        bulk=False)

``handler=None`` → the action is a pure ``TransitionService.apply`` (trigger ``user``);
otherwise ``await handler(session, entity, actor, payload)`` does the work (it may call
``TransitionService.apply`` itself). Payload models extend :class:`ActionBody`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import can
from app.core.enums import Trigger
from app.core.loading import ensure_loaded
from app.core.models import Asset, LeaseReturn, ServiceRequest, User
from app.core.schemas import ApiInput, BlockedAction
from app.core.statemachine import Transition, TransitionMap, authorizers, guards
from app.core.transitions import (
    ASSET_TRANSITIONS,
    LEASE_TRANSITIONS,
    SR_TRANSITIONS,
    transition_service,
)

type Resource = Literal["service_requests", "lease_returns", "assets"]
type ActionHandler = Callable[[AsyncSession, Any, User, "ActionBody"], Awaitable[None]]

BULK_MAX_IDS = 200


class ActionBody(ApiInput):
    """Base body of every action: flat action fields + optional ``comment`` and ``version``."""

    comment: str | None = Field(default=None, max_length=2000)
    version: int | None = None


class BulkBody(ApiInput):
    """Bulk wrapper: ``{ ids, comment?, ...actionFields }`` (extra fields validated later)."""

    model_config = {"extra": "allow"}  # noqa: RUF012
    ids: list[int] = Field(min_length=1, max_length=BULK_MAX_IDS)


@dataclass(frozen=True, slots=True)
class ResourceConfig:
    resource: Resource
    entity_type: str
    model: type
    tmap: TransitionMap
    endpoint_permission: str | None = None  # extra permission for the generic endpoint


RESOURCES: dict[str, ResourceConfig] = {
    "service_requests": ResourceConfig(
        "service_requests", "service_request", ServiceRequest, SR_TRANSITIONS
    ),
    "lease_returns": ResourceConfig(
        "lease_returns", "lease_return", LeaseReturn, LEASE_TRANSITIONS
    ),
    "assets": ResourceConfig("assets", "asset", Asset, ASSET_TRANSITIONS, "assets.update"),
}
_BY_ENTITY = {cfg.model: cfg for cfg in RESOURCES.values()}

#: SPEC_RESOLUTIONS #24: never dispatchable through the generic asset endpoint.
_NEVER_ON_ENDPOINT = {("assets", "deactivate")}


@dataclass(frozen=True, slots=True)
class ActionDef:
    resource: Resource
    name: str
    permission: str | tuple[str, ...] | None
    payload_model: type[ActionBody]
    handler: ActionHandler | None
    bulk: bool
    owner: str
    via_endpoint: bool = True

    @property
    def transition(self) -> Transition | None:
        return RESOURCES[self.resource].tmap.get(self.name)

    @property
    def requires_comment(self) -> bool:
        transition = self.transition
        return bool(transition and transition.requires_comment)


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[tuple[str, str], ActionDef] = {}

    def register(
        self,
        *,
        resource: Resource,
        name: str,
        permission: str | tuple[str, ...] | None = None,
        payload_model: type[ActionBody] | None = None,
        handler: ActionHandler | None = None,
        bulk: bool | None = None,
        owner: str = "core",
        via_endpoint: bool | None = None,
    ) -> ActionDef:
        config = RESOURCES[resource]
        transition = config.tmap.get(name)
        if transition is None and handler is None:
            raise ValueError(f"{resource}.{name}: not in the transition map; a handler is required")
        if (
            transition is not None
            and transition.internal
            and (resource, name) not in _NEVER_ON_ENDPOINT
        ):
            raise ValueError(f"{resource}.{name} is internal (import/sync/system only)")
        if resource == "assets" and handler is None:
            raise ValueError("Asset actions need a handler (AssetService.move)")
        endpoint = transition.via_endpoint if transition is not None else True
        if via_endpoint is not None:
            endpoint = via_endpoint
        if (resource, name) in _NEVER_ON_ENDPOINT:
            endpoint = False
        definition = ActionDef(
            resource=resource,
            name=name,
            permission=permission
            if permission is not None
            else (transition.permission if transition else None),
            payload_model=payload_model or ActionBody,
            handler=handler,
            bulk=bulk if bulk is not None else bool(transition and transition.bulk),
            owner=owner,
            via_endpoint=endpoint,
        )
        self._actions[(resource, name)] = definition
        return definition

    def get(self, resource: str, name: str) -> ActionDef | None:
        return self._actions.get((resource, name))

    def for_resource(self, resource: str) -> list[ActionDef]:
        order = {name: index for index, name in enumerate(RESOURCES[resource].tmap.actions)}
        defs = [d for (res, _), d in self._actions.items() if res == resource]
        return sorted(defs, key=lambda d: order.get(d.name, 10_000))

    def unregister_owner(self, owner: str) -> None:
        for key, definition in list(self._actions.items()):
            if definition.owner == owner:
                del self._actions[key]

    # --- availability -------------------------------------------------------------------------

    async def available(
        self, session: AsyncSession, entity: Any, user: User | None
    ) -> tuple[list[str], list[BlockedAction]]:
        """``(availableActions, blockedActions)`` for ``user`` (SPEC §2.4)."""
        config = _BY_ENTITY[type(entity)]
        await ensure_loaded(session, entity)
        available: list[str] = []
        blocked: list[BlockedAction] = []
        if user is None:
            return available, blocked
        for definition in self.for_resource(config.resource):
            transition = self._candidate(config, entity, definition.name, user)
            if transition is None:
                continue
            if config.endpoint_permission and not can(user, config.endpoint_permission):
                continue
            if not can(user, definition.permission) or not can(user, transition.permission):
                continue
            if not await self._authorized(
                session, config.entity_type, transition.action, entity, user
            ):
                continue
            failure = None
            for guard in guards.for_action(config.entity_type, transition.action):
                if not guard.applies(Trigger.USER, evaluating_availability=True):
                    continue
                failure = await guard.fn(session, entity, user, None)
                if failure is not None:
                    break
            if failure is None:
                available.append(definition.name)
            else:
                blocked.append(
                    BlockedAction(
                        action=definition.name, guard=failure.code, reason=failure.message
                    )
                )
        return available, blocked

    async def available_names(
        self, session: AsyncSession, entity: Any, user: User | None
    ) -> list[str]:
        return (await self.available(session, entity, user))[0]

    @staticmethod
    def _candidate(config: ResourceConfig, entity: Any, name: str, user: User) -> Transition | None:
        if config.resource == "assets":
            transition = config.tmap.get(name)
            if transition is None or not transition.allows_from(entity.status):
                return None
            return transition
        return transition_service.candidate(entity, name, user, Trigger.USER)

    @staticmethod
    async def _authorized(
        session: AsyncSession, entity_type: str, action: str, entity: Any, user: User
    ) -> bool:
        for authorizer in authorizers.for_action(entity_type, action):
            if not await authorizer.fn(session, entity, user):
                return False
        return True


actions = ActionRegistry()
