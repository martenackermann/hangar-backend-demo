"""In-process domain event bus with two phases (SPEC §2.4).

- ``in_transaction`` handlers run immediately inside the publisher's transaction:
  ``async def handler(session: AsyncSession, event: E) -> None`` — they may write rows,
  call ``AssetService.move`` or ``notify()``; an exception rolls the whole unit back.
- ``after_commit`` handlers run after the unit of work committed:
  ``async def handler(event: E) -> None`` — provider calls and other side effects that must
  not roll back. Errors are logged, never raised. Open your own session if you need the DB.

Subscribe through the slice registry (``reg.subscribe(Transitioned, fn, phase=...)``) or
:func:`subscribe` directly. Publish with ``await emit(session, event)``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import after_commit
from app.core.enums import ImportJobStatus, ImportType, Trigger

log = structlog.get_logger(__name__)

type Phase = Literal["in_transaction", "after_commit"]
type InTransactionHandler = Callable[[AsyncSession, Any], Awaitable[None]]
type AfterCommitHandler = Callable[[Any], Awaitable[None]]


# --- events (SPEC §2.4 "Domain events") --------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityKey:
    type: str
    id: int
    label: str


@dataclass(frozen=True, slots=True)
class Transitioned:
    entity_type: str
    entity_id: int
    action: str
    from_status: str | None
    to_status: str
    actor_id: int | None
    trigger: Trigger
    payload: dict[str, Any] | None = None
    comment: str | None = None
    #: The ORM entity (in-transaction handlers only; do not keep references).
    entity: Any = field(default=None, repr=False, compare=False)
    #: Typed payload model as validated by the action (``None`` for jobs/imports).
    payload_model: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class AssetMoved:
    movement_id: int
    asset_id: int
    action: str
    from_status: str | None
    to_status: str
    service_request_id: int | None
    trigger: Trigger


@dataclass(frozen=True, slots=True)
class NotificationQueued:
    notification_id: str
    template_id: str
    to: list[str]
    cc: list[str]
    entity: EntityKey | None
    trigger: Trigger
    actor_id: int | None
    attachment_file_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NotificationSent:
    notification_id: str
    subject: str
    html: str
    text: str
    message_id: str
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationFailed:
    notification_id: str
    error: str


@dataclass(frozen=True, slots=True)
class AppointmentChanged:
    appointment_id: int
    kind: str


@dataclass(frozen=True, slots=True)
class ImportFinished:
    import_job_id: int
    type: ImportType
    status: ImportJobStatus


@dataclass(frozen=True, slots=True)
class StockRecalculated:
    at: datetime


@dataclass(frozen=True, slots=True)
class UserDeactivated:
    user_id: int


@dataclass(frozen=True, slots=True)
class VipListChanged:
    user_id: str


# --- bus ---------------------------------------------------------------------------------


class EventBus:
    def __init__(self) -> None:
        self._in_tx: dict[type, list[InTransactionHandler]] = defaultdict(list)
        self._after: dict[type, list[AfterCommitHandler]] = defaultdict(list)

    def subscribe(
        self,
        event_type: type,
        handler: Callable[..., Awaitable[None]],
        *,
        phase: Phase = "after_commit",
    ) -> None:
        if phase == "in_transaction":
            self._in_tx[event_type].append(handler)
        elif phase == "after_commit":
            self._after[event_type].append(handler)
        else:  # pragma: no cover - typing prevents this
            raise ValueError(f"Unknown phase {phase!r}")

    def unsubscribe(self, event_type: type, handler: Callable[..., Awaitable[None]]) -> None:
        self._in_tx[event_type] = [h for h in self._in_tx[event_type] if h is not handler]
        self._after[event_type] = [h for h in self._after[event_type] if h is not handler]

    def handlers(self, event_type: type, phase: Phase) -> list[Callable[..., Awaitable[None]]]:
        registry = self._in_tx if phase == "in_transaction" else self._after
        return [h for cls in event_type.__mro__ for h in registry.get(cls, ())]

    async def emit(self, session: AsyncSession, event: Any) -> None:
        """Run in-transaction handlers now; schedule after-commit handlers on ``session``."""
        for handler in self.handlers(type(event), "in_transaction"):
            await handler(session, event)
        for handler in self.handlers(type(event), "after_commit"):
            after_commit(session, _bind(handler, event))

    def clear(self) -> None:
        self._in_tx.clear()
        self._after.clear()


def _bind(handler: AfterCommitHandler, event: Any) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        await handler(event)

    run.__qualname__ = getattr(handler, "__qualname__", "after_commit_handler")
    return run


bus = EventBus()


def subscribe(
    event_type: type, handler: Callable[..., Awaitable[None]], *, phase: Phase = "after_commit"
) -> None:
    bus.subscribe(event_type, handler, phase=phase)


async def emit(session: AsyncSession, event: Any) -> None:
    await bus.emit(session, event)
