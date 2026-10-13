"""TransitionService — the only way to change SR / lease return status (SPEC §4 preamble).

``apply`` in one transaction (never commits):
1. finds the transition for (current status, action) — none → ``409 transition_not_allowed``;
2. checks permission + authorizers (user trigger only) → ``403``;
3. runs core + registered guards — first failure → ``409 guard_failed`` with its code;
4. writes status, ``status_changed_at`` (version increments on flush) and the core column
   effects (chain mails set ``communication_date``, SPEC_RESOLUTIONS #9);
5. inserts ``status_transitions``;
6. emits :class:`~app.core.events.Transitioned` (in-transaction subscribers run now,
   after-commit subscribers after the unit of work commits).

Lock the row first (``load_for_update``) when acting on user input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import can
from app.core.enums import Trigger
from app.core.errors import (
    ForbiddenError,
    GuardFailed,
    NotFoundError,
    TransitionNotAllowed,
    VersionConflict,
)
from app.core.events import Transitioned, emit
from app.core.loading import ensure_loaded
from app.core.models import LeaseReturn, ServiceRequest, StatusTransition, User
from app.core.statemachine import Transition, TransitionMap, authorizers, guards
from app.core.timeutil import business_today, now_utc
from app.core.transitions.lease_map import LEASE_TRANSITIONS
from app.core.transitions.sr_map import SR_TRANSITIONS

MAPS: dict[str, TransitionMap] = {
    "service_request": SR_TRANSITIONS,
    "lease_return": LEASE_TRANSITIONS,
}
ENTITY_TYPES: dict[type, str] = {ServiceRequest: "service_request", LeaseReturn: "lease_return"}

#: SPEC_RESOLUTIONS #9: every chain mail sets ``communication_date = today``.
CHAIN_MAIL_ACTIONS: dict[str, frozenset[str]] = {
    "service_request": frozenset(
        {"send_invitation", "send_campus_info", "send_reminder_1", "send_reminder_2", "escalate"}
    ),
    "lease_return": frozenset({"send_info", "send_reminder_1", "send_reminder_2", "escalate"}),
}
_LEASE_SENT_AT = {
    "send_info": "info_sent_at",
    "send_reminder_1": "reminder_1_sent_at",
    "send_reminder_2": "reminder_2_sent_at",
    "escalate": "escalation_sent_at",
}


@dataclass(frozen=True, slots=True)
class TransitionResult:
    entity: Any
    action: str
    requested_action: str
    from_status: str | None
    to_status: str
    record: StatusTransition


def entity_type_of(entity: Any) -> str:
    try:
        return ENTITY_TYPES[type(entity)]
    except KeyError as exc:  # pragma: no cover - programming error
        raise TypeError(f"{type(entity).__name__} has no transition map") from exc


def payload_json(payload: BaseModel | dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json", by_alias=True, exclude={"comment", "version"})
    else:
        data = {k: v for k, v in payload.items() if k not in ("comment", "version")}
    return data or None


def check_version(entity: Any, version: int | None) -> None:
    """``409 version_conflict`` when the client sent a stale ``version`` (API.md §1.9)."""
    if version is not None and getattr(entity, "version", None) != version:
        raise VersionConflict(entity.version)


async def load_for_update[M](session: AsyncSession, model: type[M], entity_id: int) -> M:
    """Load ``model`` by id with ``SELECT … FOR UPDATE`` (fresh state) or 404."""
    stmt = (
        select(model)
        .where(model.id == entity_id)  # pyright: ignore[reportAttributeAccessIssue]
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    entity = (await session.scalars(stmt)).first()
    if entity is None:
        raise NotFoundError(f"{model.__name__} {entity_id} not found.")
    return entity


class TransitionService:
    def map_for(self, entity: Any) -> TransitionMap:
        return MAPS[entity_type_of(entity)]

    # --- evaluation -----------------------------------------------------------------------

    def candidate(
        self, entity: Any, action: str, actor: User | None, trigger: Trigger
    ) -> Transition | None:
        """The concrete transition ``action`` maps to from the current status, or ``None``."""
        tmap = self.map_for(entity)
        transition = tmap.get(action)
        current = entity.status
        if transition is None or not transition.allows_from(current):
            return None
        if transition.resolve is not None:
            resolved_name = transition.resolve(entity)
            resolved = tmap.get(resolved_name) if resolved_name else None
            if resolved is None:
                return None
            transition = resolved
        if not transition.allows_from(current) or trigger not in transition.triggers:
            return None
        if transition.condition is not None and not transition.condition(entity, actor):
            return None
        return transition

    def allowed_actions(self, entity: Any, actor: User | None) -> list[str]:
        """User actions allowed by status (+condition) and permission — guards not evaluated."""
        result: list[str] = []
        for transition in self.map_for(entity).from_status(entity.status):
            if transition.internal:
                continue
            if self.candidate(entity, transition.action, actor, Trigger.USER) is None:
                continue
            if can(actor, transition.permission):
                result.append(transition.action)
        return result

    # --- mutation -------------------------------------------------------------------------

    async def apply(
        self,
        session: AsyncSession,
        entity: Any,
        action: str,
        *,
        actor: User | None,
        trigger: Trigger,
        payload: BaseModel | dict[str, Any] | None = None,
        comment: str | None = None,
    ) -> TransitionResult:
        entity_type = entity_type_of(entity)
        tmap = MAPS[entity_type]
        await ensure_loaded(session, entity)
        current: str | None = entity.status
        requested = tmap.get(action)
        transition = self.candidate(entity, action, actor, trigger)
        if transition is None:
            reason = (
                "is unknown"
                if requested is None
                else f"is not allowed while the status is '{current}'"
            )
            raise TransitionNotAllowed(
                f"Action '{action}' {reason}.",
                action=action,
                current_status=current,
                allowed_actions=self.allowed_actions(entity, actor),
            )

        if trigger == Trigger.USER:
            if actor is None:
                raise ForbiddenError("A user is required for this action.")
            for perm in {requested.permission if requested else None, transition.permission}:
                if perm is not None and not can(actor, perm):
                    keys = [perm] if isinstance(perm, str) else list(perm)
                    raise ForbiddenError(
                        "You do not have permission to perform this action.",
                        requiredPermissions=keys,
                    )
            for authorizer in authorizers.for_action(entity_type, transition.action):
                if not await authorizer.fn(session, entity, actor):
                    raise ForbiddenError(authorizer.message, code=authorizer.code)

        guard_payload = payload
        for guard in guards.for_action(entity_type, transition.action):
            if not guard.applies(trigger, evaluating_availability=False):
                continue
            failure = await guard.fn(
                session,
                entity,
                actor,
                guard_payload if isinstance(guard_payload, BaseModel) else None,
            )
            if failure is not None:
                raise GuardFailed(failure.code, failure.message)

        to_status = transition.target(entity, current)
        now = now_utc()
        if to_status != current:
            entity.status = to_status
            entity.status_changed_at = now
        if transition.action in CHAIN_MAIL_ACTIONS.get(entity_type, frozenset()):
            entity.communication_date = business_today(now)
            if entity_type == "lease_return":
                setattr(entity, _LEASE_SENT_AT[transition.action], now)

        if entity.id is None:
            await session.flush()
        entity_id = cast(int, entity.id)
        record = StatusTransition(
            entity_type=entity_type,
            entity_id=entity_id,
            action=transition.action,
            from_status=current,
            to_status=to_status,
            actor_user_id=actor.id if actor is not None else None,
            trigger=trigger,
            comment=comment if comment is not None else getattr(payload, "comment", None),
            payload=payload_json(payload),
            occurred_at=now,
        )
        session.add(record)
        await session.flush()
        await emit(
            session,
            Transitioned(
                entity_type=entity_type,
                entity_id=entity_id,
                action=transition.action,
                from_status=current,
                to_status=to_status,
                actor_id=record.actor_user_id,
                trigger=trigger,
                payload=record.payload,
                comment=record.comment,
                entity=entity,
                payload_model=payload if isinstance(payload, BaseModel) else None,
            ),
        )
        return TransitionResult(entity, transition.action, action, current, to_status, record)


transition_service = TransitionService()
