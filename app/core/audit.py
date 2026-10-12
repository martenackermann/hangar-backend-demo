"""Audit trail sink.

:meth:`app.core.workflow.Workflow.apply` (and any service that changes something worth
auditing) calls :func:`record_audit`. The sink is pluggable:

- default: :class:`LogAuditSink` writes a structured log line;
- tests: :class:`MemoryAuditSink` collects records;
- phase 2b: a database sink adds an ``audit_log`` row through ``ctx["session"]``
  (registered with :func:`set_audit_sink` at import time of the core module).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

log = structlog.get_logger("hangar.audit")

_JSON_SCALARS = (str, int, float, bool, type(None))


@dataclass(frozen=True, slots=True)
class AuditRecord:
    entity_type: str
    entity_id: str
    action: str
    actor_id: str | None
    from_state: str | None = None
    to_state: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AuditSink(Protocol):
    async def record(self, record: AuditRecord, ctx: Mapping[str, Any]) -> None: ...


class LogAuditSink:
    async def record(self, record: AuditRecord, ctx: Mapping[str, Any]) -> None:
        log.info(
            "audit",
            entity_type=record.entity_type,
            entity_id=record.entity_id,
            action=record.action,
            actor_id=record.actor_id,
            from_state=record.from_state,
            to_state=record.to_state,
            data=dict(record.data),
        )


class MemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def record(self, record: AuditRecord, ctx: Mapping[str, Any]) -> None:
        self.records.append(record)


_sink: AuditSink = LogAuditSink()


def get_audit_sink() -> AuditSink:
    return _sink


def set_audit_sink(sink: AuditSink) -> AuditSink:
    """Install ``sink``; returns the previous one (handy for tests)."""
    global _sink
    previous, _sink = _sink, sink
    return previous


def json_safe(values: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only JSON-friendly context values (drops sessions, ORM objects, ...)."""
    safe: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, _JSON_SCALARS):
            safe[key] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(v, _JSON_SCALARS) for v in value):
            safe[key] = list(value)
        elif isinstance(value, datetime):
            safe[key] = value.isoformat()
    return safe


async def record_audit(record: AuditRecord, ctx: Mapping[str, Any] | None = None) -> None:
    await _sink.record(record, ctx or {})
