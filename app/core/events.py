"""In-process domain event bus.

Modules publish events (dataclasses) and other modules react without importing each
other's services. Subscribers live in ``app/modules/<name>/subscribers.py`` (imported by
auto-discovery at startup, in the API and in the worker)::

    from app.core.events import on_transition

    @on_transition("service_request", to_state="shipped")
    async def queue_shipping_mail(event: TransitionEvent, ctx: EventContext) -> None:
        on_commit(ctx["session"], lambda: send_mail.defer_async(sr_id=event.entity_id))

Handlers run inline, in subscription order, inside the publisher's transaction. Keep
them fast; defer slow work (mail, HTTP) to a job, ideally via ``on_commit``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Any

import structlog

log = structlog.get_logger(__name__)

type EventContext = Mapping[str, Any]
type Handler = Callable[[Any, EventContext], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class TransitionEvent:
    """Emitted by :meth:`app.core.workflow.Workflow.apply` after a state change."""

    workflow: str
    entity_id: Any
    action: str
    from_state: str
    to_state: str
    actor_id: str | None
    entity: Any = field(repr=False, compare=False)
    data: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[tuple[Callable[[Any], bool], Handler]]] = defaultdict(list)

    def subscribe(
        self,
        event_type: type,
        handler: Handler,
        *,
        when: Callable[[Any], bool] | None = None,
    ) -> Handler:
        self._handlers[event_type].append((when or (lambda _event: True), handler))
        return handler

    def unsubscribe(self, event_type: type, handler: Handler) -> None:
        self._handlers[event_type] = [
            pair for pair in self._handlers[event_type] if pair[1] is not handler
        ]

    async def publish(self, event: Any, ctx: EventContext | None = None) -> None:
        context: EventContext = ctx or {}
        for event_type in type(event).__mro__:
            for predicate, handler in list(self._handlers.get(event_type, ())):
                if not predicate(event):
                    continue
                result = handler(event, context)
                if isawaitable(result):
                    await result

    def clear(self) -> None:
        self._handlers.clear()


bus = EventBus()


def subscribe(event_type: type, *, when: Callable[[Any], bool] | None = None):
    """Decorator registering a handler on the global bus."""

    def decorator(handler: Handler) -> Handler:
        return bus.subscribe(event_type, handler, when=when)

    return decorator


def on_transition(
    workflow: str,
    *,
    action: str | None = None,
    to_state: str | None = None,
    from_state: str | None = None,
):
    """Decorator for :class:`TransitionEvent` handlers filtered by workflow/action/state."""

    def predicate(event: TransitionEvent) -> bool:
        return (
            event.workflow == workflow
            and (action is None or event.action == action)
            and (to_state is None or event.to_state == to_state)
            and (from_state is None or event.from_state == from_state)
        )

    return subscribe(TransitionEvent, when=predicate)
