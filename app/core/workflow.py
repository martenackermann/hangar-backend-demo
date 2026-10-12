"""Generic state-machine helper for entity lifecycles.

Define states and actions as ``StrEnum`` and a transition map
``{from_state: {action: to_state}}``; the :class:`Workflow` validates transitions,
checks permissions and guards, mutates the entity's state attribute, writes an audit
record and publishes a :class:`~app.core.events.TransitionEvent`::

    class SrState(StrEnum): IMPORTED = "imported"; INVITED = "invited"; ...
    class SrAction(StrEnum): INVITE = "invite"; CANCEL = "cancel"; ...

    SR_WORKFLOW = Workflow[ServiceRequest, SrState, SrAction](
        "service_request",
        {
            SrState.IMPORTED: {SrAction.INVITE: SrState.INVITED, SrAction.CANCEL: SrState.CANCELLED},
            SrState.INVITED: {SrAction.CANCEL: SrState.CANCELLED},
            SrState.CANCELLED: {},
        },
        permissions={SrAction.INVITE: "service_requests.communicate"},
        guards={SrAction.INVITE: [has_contact_email]},
    )

    await SR_WORKFLOW.apply(sr, SrAction.INVITE, user, session=session, comment="...")

Guards are synchronous ``(entity, actor, ctx) -> bool | str | None``: ``True``/``None``
allow, ``False`` denies with a generic message, a ``str`` denies with that reason. Load
whatever a guard needs before calling ``apply`` (pass it via ``ctx``). ``actor=None``
means "system" (jobs) and skips permission checks, not guards.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from enum import StrEnum
from inspect import isawaitable
from typing import Any, Protocol

from app.core.audit import AuditRecord, json_safe, record_audit
from app.core.errors import ApiError, PermissionDeniedError, TransitionError
from app.core.events import EventBus, TransitionEvent
from app.core.events import bus as default_bus
from app.core.schemas import BulkFailure, BulkResult

type TransitionMap[S, A] = Mapping[S, Mapping[A, S]]
type GuardResult = bool | str | None
type Guard[E] = Callable[[E, Actor | None, Mapping[str, Any]], GuardResult]
type Effect[E] = Callable[[E, TransitionEvent, Mapping[str, Any]], Awaitable[None] | None]


class Actor(Protocol):
    """Anything with an id and permission check (e.g. ``CurrentUser``)."""

    @property
    def id(self) -> str: ...

    def has_permission(self, permission: str) -> bool: ...


def _as_list[T](value: Sequence[T] | T) -> list[T]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value]  # pyright: ignore[reportReturnType]


class Workflow[E, S: StrEnum, A: StrEnum]:
    def __init__(
        self,
        name: str,
        transitions: TransitionMap[S, A],
        *,
        state_attr: str = "status",
        id_attr: str = "id",
        permissions: Mapping[A, str] | None = None,
        guards: Mapping[A, Sequence[Guard[E]] | Guard[E]] | None = None,
        effects: Mapping[A, Sequence[Effect[E]] | Effect[E]] | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        if not transitions:
            raise ValueError(f"Workflow {name!r} needs at least one state.")
        self.name = name
        self.transitions: dict[S, dict[A, S]] = {
            state: dict(actions) for state, actions in transitions.items()
        }
        self.state_attr = state_attr
        self.id_attr = id_attr
        self.permissions: dict[A, str] = dict(permissions or {})
        self.guards: dict[A, list[Guard[E]]] = {
            action: _as_list(g) for action, g in (guards or {}).items()
        }
        self.effects: dict[A, list[Effect[E]]] = {
            action: _as_list(e) for action, e in (effects or {}).items()
        }
        self._bus = event_bus or default_bus
        self._state_type: type[S] = type(next(iter(self.transitions)))
        self._action_type: type[A] | None = None
        self._validate()

    # --- definition ----------------------------------------------------------------

    def _validate(self) -> None:
        actions = {a for targets in self.transitions.values() for a in targets}
        if actions:
            self._action_type = type(next(iter(actions)))
        for state, targets in self.transitions.items():
            if not isinstance(state, self._state_type):
                raise ValueError(
                    f"{self.name}: state {state!r} is not a {self._state_type.__name__}"
                )
            for action, target in targets.items():
                if self._action_type is not None and not isinstance(action, self._action_type):
                    raise ValueError(f"{self.name}: action {action!r} has the wrong type")
                if target not in self.transitions:
                    raise ValueError(
                        f"{self.name}: '{state}' --{action}--> '{target}' targets a state "
                        "that is not a key of the transition map (add it, e.g. with {})."
                    )
        for label, mapping in (
            ("permissions", self.permissions),
            ("guards", self.guards),
            ("effects", self.effects),
        ):
            unknown = set(mapping) - actions
            if unknown:
                raise ValueError(
                    f"{self.name}: {label} reference unknown actions {sorted(unknown)}"
                )

    @property
    def states(self) -> list[S]:
        return list(self.transitions)

    @property
    def actions(self) -> list[A]:
        seen: dict[A, None] = {}
        for targets in self.transitions.values():
            seen.update(dict.fromkeys(targets))
        return list(seen)

    @property
    def terminal_states(self) -> list[S]:
        return [state for state, targets in self.transitions.items() if not targets]

    def target(self, state: S | str, action: A | str) -> S | None:
        coerced = self._parse_action(action)
        if coerced is None:
            return None
        return self.transitions.get(self._coerce_state(state), {}).get(coerced)

    def to_mermaid(self) -> str:
        """``stateDiagram-v2`` source, e.g. for docs."""
        lines = ["stateDiagram-v2"]
        for state, targets in self.transitions.items():
            lines.extend(
                f"    {state} --> {target}: {action}" for action, target in targets.items()
            )
        lines.extend(f"    {state} --> [*]" for state in self.terminal_states)
        return "\n".join(lines)

    # --- evaluation ----------------------------------------------------------------

    def _coerce_state(self, value: S | str) -> S:
        return value if isinstance(value, self._state_type) else self._state_type(value)

    def _parse_action(self, value: A | str) -> A | None:
        """The action enum member for ``value``; ``None`` if this workflow has no such action."""
        if self._action_type is None:
            return None
        if isinstance(value, self._action_type):
            return value
        try:
            return self._action_type(value)
        except ValueError:
            return None

    def state_of(self, entity: E) -> S:
        return self._coerce_state(getattr(entity, self.state_attr))

    def _entity_id(self, entity: E) -> Any:
        return getattr(entity, self.id_attr, None)

    def _not_allowed(
        self, entity: E, action: str, actor: Actor | None, ctx: Mapping[str, Any]
    ) -> TransitionError:
        current = self.state_of(entity)
        return TransitionError(
            f"Action '{action}' is not allowed while {self.name} is '{current}'.",
            action=action,
            current_state=str(current),
            allowed_actions=[str(a) for a in self.available_actions(entity, actor, **ctx)],
            entity=self.name,
            entity_id=self._entity_id(entity),
        )

    def _denial(
        self, entity: E, raw_action: A | str, actor: Actor | None, ctx: Mapping[str, Any]
    ) -> ApiError | None:
        action = self._parse_action(raw_action)
        if action is None:
            return self._not_allowed(entity, str(raw_action), actor, ctx)
        current = self.state_of(entity)
        permission = self.permissions.get(action)
        if permission and actor is not None and not actor.has_permission(permission):
            return PermissionDeniedError(
                f"You are not allowed to '{action}' this {self.name}.",
                requiredPermissions=[permission],
            )
        if action not in self.transitions.get(current, {}):
            return self._not_allowed(entity, str(action), actor, ctx)
        for guard in self.guards.get(action, ()):
            result = guard(entity, actor, ctx)
            if result is True or result is None:
                continue
            reason = (
                result if isinstance(result, str) else f"Preconditions for '{action}' are not met."
            )
            return TransitionError(
                reason,
                action=str(action),
                current_state=str(current),
                allowed_actions=[
                    str(a) for a in self.available_actions(entity, actor, **ctx) if a != action
                ],
                entity=self.name,
                entity_id=self._entity_id(entity),
            )
        return None

    def check(self, entity: E, action: A | str, actor: Actor | None = None, **ctx: Any) -> None:
        """Raise the error :meth:`apply` would raise, without changing anything."""
        if (error := self._denial(entity, action, actor, ctx)) is not None:
            raise error

    def can(self, entity: E, action: A | str, actor: Actor | None = None, **ctx: Any) -> bool:
        return self._denial(entity, action, actor, ctx) is None

    def available_actions(self, entity: E, user: Actor | None = None, **ctx: Any) -> list[A]:
        """Actions valid for the entity's state that ``user`` may perform and guards allow."""
        current = self.state_of(entity)
        result: list[A] = []
        for action in self.transitions.get(current, {}):
            permission = self.permissions.get(action)
            if permission and user is not None and not user.has_permission(permission):
                continue
            if all(g(entity, user, ctx) in (True, None) for g in self.guards.get(action, ())):
                result.append(action)
        return result

    # --- mutation ------------------------------------------------------------------

    async def apply(
        self, entity: E, action: A | str, actor: Actor | None, **ctx: Any
    ) -> TransitionEvent:
        """Validate and perform ``action``; returns the published :class:`TransitionEvent`.

        ``ctx`` is passed to guards, effects, the audit sink and event handlers. Pass the
        ``session`` (needed by DB-backed sinks/handlers) and any audit-worthy details
        (``comment=...``); JSON-friendly values end up in the audit record.
        """
        if (error := self._denial(entity, action, actor, ctx)) is not None:
            raise error
        parsed = self._parse_action(action)
        assert parsed is not None  # guaranteed by _denial
        current = self.state_of(entity)
        target = self.transitions[current][parsed]
        setattr(entity, self.state_attr, target)

        event = TransitionEvent(
            workflow=self.name,
            entity_id=self._entity_id(entity),
            action=str(parsed),
            from_state=str(current),
            to_state=str(target),
            actor_id=actor.id if actor is not None else None,
            entity=entity,
            data=json_safe(ctx),
        )
        for effect in self.effects.get(parsed, ()):
            result = effect(entity, event, ctx)
            if isawaitable(result):
                await result
        await record_audit(
            AuditRecord(
                entity_type=self.name,
                entity_id=str(event.entity_id),
                action=event.action,
                actor_id=event.actor_id,
                from_state=event.from_state,
                to_state=event.to_state,
                data=event.data,
                occurred_at=event.occurred_at,
            ),
            ctx,
        )
        await self._bus.publish(event, ctx)
        return event

    async def apply_bulk(
        self, entities: Iterable[E], action: A | str, actor: Actor | None, **ctx: Any
    ) -> BulkResult:
        """Apply to each entity independently; per-item failures are collected, not raised."""
        result = BulkResult()
        for entity in entities:
            entity_id = self._entity_id(entity)
            try:
                await self.apply(entity, action, actor, **ctx)
            except ApiError as exc:
                result.failed.append(
                    BulkFailure(
                        id=entity_id,
                        status=exc.status,
                        title=exc.resolved_title,
                        detail=exc.detail,
                    )
                )
            else:
                result.succeeded.append(entity_id)
        return result
