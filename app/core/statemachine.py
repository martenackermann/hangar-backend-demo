"""Generic state-machine building blocks (SPEC §2.4, §4).

A :class:`TransitionMap` is a list of :class:`Transition` rows — exactly the tables of SPEC
§4.1.1 / §4.2 / §4.3. Guards (:class:`Guard`) are attached per ``(entity_type, action)`` in
the :data:`guards` registry: core guards are registered by ``app.core.transitions``, slice
guards through ``SliceRegistry.register_guard``. Authorizers (SPEC_RESOLUTIONS #2, e.g.
tour ownership) live in :data:`authorizers`: a failing authorizer hides the action from
``availableActions``; calling it anyway returns ``403`` with the authorizer's code.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.enums import Trigger

if TYPE_CHECKING:
    from pydantic import BaseModel
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.models import User

#: Pseudo status for creation transitions (``∅`` in the spec tables).
NEW = "__new__"

ALL_TRIGGERS = frozenset(Trigger)
USER = frozenset({Trigger.USER})


@dataclass(frozen=True, slots=True)
class GuardFailure:
    code: str
    message: str


type GuardFn = Callable[
    ["AsyncSession", Any, "User | None", "BaseModel | None"], Awaitable[GuardFailure | None]
]
type AuthorizerFn = Callable[["AsyncSession", Any, "User"], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class Guard:
    """``fn(session, entity, actor, payload) -> GuardFailure | None``.

    ``triggers``: evaluated only for these triggers (``None`` = all).
    ``needs_payload``: skipped when computing ``availableActions`` (no payload yet).
    """

    code: str
    fn: GuardFn
    triggers: frozenset[Trigger] | None = None
    needs_payload: bool = False
    owner: str = "core"

    def applies(self, trigger: Trigger, *, evaluating_availability: bool) -> bool:
        if evaluating_availability and self.needs_payload:
            return False
        return self.triggers is None or trigger in self.triggers


@dataclass(frozen=True, slots=True)
class Transition:
    """One row of a transition table.

    ``to``: target status, a callable ``entity -> status``, or ``None`` = unchanged
    (non-status actions such as ``pause_communication``).
    ``permission``: key, tuple of keys (any of), or ``None`` (system-only).
    ``condition``: extra from-rule ``(entity, actor) -> bool`` (e.g. claim take-over);
    failing it means "not allowed from here", not a guard failure.
    ``resolve``: virtual actions (``send_next_mail``) map the entity to a concrete action.
    ``internal``: only for import/sync/system triggers (never offered to users).
    ``via_endpoint``: ``False`` for actions executed by a dedicated endpoint (``ship``).
    """

    action: str
    from_: frozenset[str]
    to: str | Callable[[Any], str] | None
    permission: str | tuple[str, ...] | None
    triggers: frozenset[Trigger]
    label: str
    owner: str
    bulk: bool = False
    requires_comment: bool = False
    destructive: bool = False
    internal: bool = False
    via_endpoint: bool = True
    condition: Callable[[Any, User | None], bool] | None = field(default=None, repr=False)
    resolve: Callable[[Any], str | None] | None = field(default=None, repr=False)

    def allows_from(self, status: str | None) -> bool:
        return (status or NEW) in self.from_

    def target(self, entity: Any, current: str | None) -> str:
        if self.to is None:
            assert current is not None
            return current
        if callable(self.to):
            return self.to(entity)
        return self.to


class TransitionMap:
    def __init__(
        self, entity_type: str, transitions: Iterable[Transition], status_attr: str = "status"
    ) -> None:
        self.entity_type = entity_type
        self.status_attr = status_attr
        self._by_action: dict[str, Transition] = {}
        for transition in transitions:
            if transition.action in self._by_action:
                raise ValueError(f"{entity_type}: duplicate action {transition.action!r}")
            self._by_action[transition.action] = transition

    def __iter__(self):
        return iter(self._by_action.values())

    def __contains__(self, action: object) -> bool:
        return action in self._by_action

    def get(self, action: str) -> Transition | None:
        return self._by_action.get(action)

    @property
    def actions(self) -> list[str]:
        return list(self._by_action)

    def from_status(self, status: str | None) -> list[Transition]:
        return [t for t in self._by_action.values() if t.allows_from(status)]


# --- registries ----------------------------------------------------------------------------


class GuardRegistry:
    def __init__(self) -> None:
        self._guards: dict[tuple[str, str], list[Guard]] = defaultdict(list)

    def add(self, entity_type: str, actions: str | Collection[str], guard: Guard) -> None:
        for action in [actions] if isinstance(actions, str) else actions:
            self._guards[(entity_type, action)].append(guard)

    def for_action(self, entity_type: str, action: str) -> list[Guard]:
        return list(self._guards.get((entity_type, action), ()))

    def codes(self, entity_type: str, action: str) -> list[str]:
        return [g.code for g in self.for_action(entity_type, action)]

    def remove_owner(self, owner: str) -> None:
        for key, guards in list(self._guards.items()):
            self._guards[key] = [g for g in guards if g.owner != owner]


@dataclass(frozen=True, slots=True)
class Authorizer:
    """``fn(session, entity, actor) -> bool``; ``False`` → hidden / ``403 <code>``."""

    fn: AuthorizerFn
    code: str = "forbidden"
    message: str = "You are not allowed to perform this action on this record."
    owner: str = "core"


class AuthorizerRegistry:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], list[Authorizer]] = defaultdict(list)

    def add(self, entity_type: str, actions: str | Collection[str], authorizer: Authorizer) -> None:
        for action in [actions] if isinstance(actions, str) else actions:
            self._items[(entity_type, action)].append(authorizer)

    def for_action(self, entity_type: str, action: str) -> list[Authorizer]:
        return list(self._items.get((entity_type, action), ()))

    def remove_owner(self, owner: str) -> None:
        for key, items in list(self._items.items()):
            self._items[key] = [a for a in items if a.owner != owner]


guards = GuardRegistry()
authorizers = AuthorizerRegistry()
