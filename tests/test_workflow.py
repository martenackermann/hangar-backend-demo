"""Workflow helper, exercised with a toy door state machine."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import pytest

from app.core.audit import MemoryAuditSink
from app.core.errors import PermissionDeniedError, TransitionError
from app.core.events import EventBus, TransitionEvent
from app.core.security import CurrentUser
from app.core.workflow import Actor, Workflow


class DoorState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    LOCKED = "locked"
    BROKEN = "broken"


class DoorAction(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    LOCK = "lock"
    UNLOCK = "unlock"
    BREAK = "break"


@dataclass
class Door:
    id: int
    status: DoorState | str = DoorState.CLOSED
    has_key: bool = True
    log: list[str] = field(default_factory=list)


def needs_key(door: Door, actor: Actor | None, ctx: Mapping[str, Any]) -> str | None:
    return None if door.has_key else "The door has no key."


def remember(door: Door, event: TransitionEvent, ctx: Mapping[str, Any]) -> None:
    door.log.append(f"{event.from_state}->{event.to_state}")


def make_workflow(bus: EventBus) -> Workflow[Door, DoorState, DoorAction]:
    return Workflow[Door, DoorState, DoorAction](
        "door",
        {
            DoorState.CLOSED: {
                DoorAction.OPEN: DoorState.OPEN,
                DoorAction.LOCK: DoorState.LOCKED,
                DoorAction.BREAK: DoorState.BROKEN,
            },
            DoorState.OPEN: {DoorAction.CLOSE: DoorState.CLOSED},
            DoorState.LOCKED: {DoorAction.UNLOCK: DoorState.CLOSED},
            DoorState.BROKEN: {},
        },
        permissions={DoorAction.BREAK: "doors.break"},
        guards={DoorAction.LOCK: needs_key, DoorAction.UNLOCK: [needs_key]},
        effects={DoorAction.OPEN: remember},
        event_bus=bus,
    )


VIEWER = CurrentUser(
    id="u-viewer", roles=frozenset({"viewer"}), permissions=frozenset({"doors.read"})
)
ADMIN = CurrentUser(id="u-admin", roles=frozenset({"admin"}), permissions=frozenset({"*"}))


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def workflow(bus: EventBus) -> Workflow[Door, DoorState, DoorAction]:
    return make_workflow(bus)


def test_definition_introspection(workflow: Workflow[Door, DoorState, DoorAction]) -> None:
    assert workflow.states == list(DoorState)
    assert set(workflow.actions) == set(DoorAction)
    assert workflow.terminal_states == [DoorState.BROKEN]
    assert workflow.target("closed", "open") is DoorState.OPEN
    assert workflow.target(DoorState.OPEN, DoorAction.LOCK) is None
    assert workflow.target("open", "fly") is None
    mermaid = workflow.to_mermaid()
    assert mermaid.startswith("stateDiagram-v2")
    assert "closed --> open: open" in mermaid
    assert "broken --> [*]" in mermaid


def test_available_actions_respect_state_permissions_and_guards(
    workflow: Workflow[Door, DoorState, DoorAction],
) -> None:
    door = Door(id=1)
    assert workflow.available_actions(door) == [
        DoorAction.OPEN,
        DoorAction.LOCK,
        DoorAction.BREAK,
    ]
    assert workflow.available_actions(door, VIEWER) == [DoorAction.OPEN, DoorAction.LOCK]
    assert workflow.available_actions(door, ADMIN) == [
        DoorAction.OPEN,
        DoorAction.LOCK,
        DoorAction.BREAK,
    ]
    keyless = Door(id=2, has_key=False)
    assert workflow.available_actions(keyless, VIEWER) == [DoorAction.OPEN]
    assert workflow.can(door, "lock", VIEWER)
    assert not workflow.can(keyless, DoorAction.LOCK, VIEWER)


async def test_apply_changes_state_emits_event_and_audit(
    workflow: Workflow[Door, DoorState, DoorAction],
    bus: EventBus,
    audit_sink: MemoryAuditSink,
) -> None:
    received: list[tuple[TransitionEvent, Mapping[str, Any]]] = []

    async def handler(event: TransitionEvent, ctx: Mapping[str, Any]) -> None:
        received.append((event, ctx))

    bus.subscribe(TransitionEvent, handler)
    door = Door(id=7)
    marker = object()

    event = await workflow.apply(door, "open", VIEWER, comment="airing", session=marker)

    assert door.status is DoorState.OPEN
    assert door.log == ["closed->open"]  # effect ran
    assert (event.workflow, event.entity_id, event.action) == ("door", 7, "open")
    assert (event.from_state, event.to_state, event.actor_id) == ("closed", "open", "u-viewer")
    assert event.data == {"comment": "airing"}  # non-JSON ctx values are dropped
    assert received == [(event, {"comment": "airing", "session": marker})]

    [record] = audit_sink.records
    assert (record.entity_type, record.entity_id, record.action) == ("door", "7", "open")
    assert (record.from_state, record.to_state, record.actor_id) == ("closed", "open", "u-viewer")
    assert record.data == {"comment": "airing"}


async def test_on_transition_filters_events(
    workflow: Workflow[Door, DoorState, DoorAction], bus: EventBus
) -> None:
    seen: list[str] = []
    bus.subscribe(
        TransitionEvent,
        lambda event, ctx: seen.append(event.action),
        when=lambda event: event.to_state == "closed",
    )
    door = Door(id=8)
    await workflow.apply(door, DoorAction.OPEN, VIEWER)
    await workflow.apply(door, DoorAction.CLOSE, VIEWER)
    assert seen == ["close"]


async def test_invalid_transition_raises_409_with_allowed_actions(
    workflow: Workflow[Door, DoorState, DoorAction], audit_sink: MemoryAuditSink
) -> None:
    door = Door(id=3, status=DoorState.OPEN)
    with pytest.raises(TransitionError) as caught:
        await workflow.apply(door, DoorAction.LOCK, VIEWER)
    error = caught.value
    assert error.status == 409
    assert error.current_state == "open"
    assert error.allowed_actions == ["close"]
    assert error.extensions["entityId"] == 3
    assert door.status is DoorState.OPEN
    assert audit_sink.records == []


async def test_unknown_action_is_a_transition_error(
    workflow: Workflow[Door, DoorState, DoorAction],
) -> None:
    with pytest.raises(TransitionError, match="'fly'"):
        await workflow.apply(Door(id=1), "fly", None)


async def test_guard_reason_is_reported(
    workflow: Workflow[Door, DoorState, DoorAction],
) -> None:
    door = Door(id=4, has_key=False)
    with pytest.raises(TransitionError, match="no key") as caught:
        await workflow.apply(door, DoorAction.LOCK, VIEWER)
    assert "lock" not in caught.value.allowed_actions
    assert door.status is DoorState.CLOSED


async def test_permission_denied_is_403_but_system_actor_bypasses(
    workflow: Workflow[Door, DoorState, DoorAction],
) -> None:
    door = Door(id=5)
    with pytest.raises(PermissionDeniedError) as caught:
        await workflow.apply(door, DoorAction.BREAK, VIEWER)
    assert caught.value.status == 403
    assert caught.value.extensions["requiredPermissions"] == ["doors.break"]

    event = await workflow.apply(door, DoorAction.BREAK, None)
    assert event.actor_id is None
    assert door.status is DoorState.BROKEN
    assert workflow.available_actions(door, ADMIN) == []


async def test_apply_bulk_collects_per_item_outcomes(
    workflow: Workflow[Door, DoorState, DoorAction],
) -> None:
    doors = [Door(id=1), Door(id=2, status=DoorState.LOCKED), Door(id=3, has_key=False)]
    result = await workflow.apply_bulk(doors, DoorAction.LOCK, VIEWER)
    assert result.succeeded == [1]
    assert [(f.id, f.status) for f in result.failed] == [(2, 409), (3, 409)]
    assert result.model_dump()["failed"][1]["detail"] == "The door has no key."


def test_definition_errors_are_caught_early() -> None:
    with pytest.raises(ValueError, match="not a key of the transition map"):
        Workflow[Door, DoorState, DoorAction](
            "bad", {DoorState.CLOSED: {DoorAction.OPEN: DoorState.OPEN}}
        )
    with pytest.raises(ValueError, match="unknown actions"):
        Workflow[Door, DoorState, DoorAction](
            "bad",
            {DoorState.CLOSED: {DoorAction.OPEN: DoorState.CLOSED}},
            permissions={DoorAction.LOCK: "x"},
        )
    with pytest.raises(ValueError, match="at least one state"):
        Workflow[Door, DoorState, DoorAction]("bad", {})


async def test_state_stored_as_plain_string_is_coerced(
    workflow: Workflow[Door, DoorState, DoorAction],
) -> None:
    door = Door(id=9, status="locked")  # e.g. loaded from a String column
    assert workflow.state_of(door) is DoorState.LOCKED
    await workflow.apply(door, "unlock", VIEWER)
    assert door.status is DoorState.CLOSED
