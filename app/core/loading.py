"""Async-safe relationship loading.

Objects created in the current session (or loaded with only FK ids set) have unloaded
relationships; touching them in async code raises ``MissingGreenlet``. Core calls
:func:`ensure_loaded` before evaluating guards, transitions and serializers; slice code
can call it too.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Asset, LeaseReturn, ServiceRequest

DEFAULT_RELATIONS: dict[type, tuple[str, ...]] = {
    ServiceRequest: (
        "items",
        "contact",
        "region",
        "region_rule",
        "supplying_site",
        "editor",
        "deviation_code",
    ),
    LeaseReturn: ("contact", "service_request"),
    Asset: ("article", "location", "service_request", "assigned_contact"),
}


async def ensure_loaded(session: AsyncSession, entity: Any, *names: str) -> None:
    """Load the given (default: per-model) relationships if they are not loaded yet."""
    state = inspect(entity)
    if state.transient or state.pending:
        return
    wanted = names or DEFAULT_RELATIONS.get(type(entity), ())
    missing = [name for name in wanted if name in state.unloaded]
    if missing:
        await session.refresh(entity, missing)
