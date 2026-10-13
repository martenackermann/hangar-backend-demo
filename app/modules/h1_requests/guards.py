"""H1-owned guards (SPEC_RESOLUTIONS #3/#33) — evaluated for the ``job`` trigger only.

Manual sends ignore the due rule, the pause and the master switches (SPEC §6.3 "Manual
sends"), so these guards never show up in ``blockedActions``; the dispatch job uses them
as a safety net after its SQL pre-selection (#10).

| code | message |
|---|---|
| ``chain_due`` | The next mail of the chain is not due yet. |
| ``not_paused`` | Communication is paused. |
| ``retirement_window`` | The retirement date is outside the info mail window. |
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import LeaseReturn, User
from app.core.statemachine import GuardFailure
from app.core.timeutil import business_today

from .chain import is_due, is_paused, load_settings, next_step, retirement_window_open

GUARD_MESSAGES = {
    "chain_due": "The next mail of the chain is not due yet.",
    "not_paused": "Communication is paused.",
    "retirement_window": "The retirement date is outside the info mail window.",
}


def _entity_type(entity: Any) -> str:
    return "lease_return" if isinstance(entity, LeaseReturn) else "service_request"


async def chain_due(
    session: AsyncSession, entity: Any, _actor: User | None, _payload: BaseModel | None
) -> GuardFailure | None:
    step = next_step(_entity_type(entity), entity)
    if step is None:
        return None
    settings = await load_settings(session)
    if is_due(entity, step, settings, business_today()):
        return None
    return GuardFailure("chain_due", GUARD_MESSAGES["chain_due"])


async def not_paused(
    _session: AsyncSession, entity: Any, _actor: User | None, _payload: BaseModel | None
) -> GuardFailure | None:
    if is_paused(entity):
        return GuardFailure("not_paused", GUARD_MESSAGES["not_paused"])
    return None


async def retirement_window(
    session: AsyncSession, lease: LeaseReturn, _actor: User | None, _payload: BaseModel | None
) -> GuardFailure | None:
    settings = await load_settings(session)
    if retirement_window_open(lease, settings, business_today()):
        return None
    return GuardFailure("retirement_window", GUARD_MESSAGES["retirement_window"])


# --- authorizers (hide the first-mail variant that does not apply) ---------------------------


async def campus_request(_session: AsyncSession, sr: Any, _actor: User) -> bool:
    """``send_campus_info`` is offered for campus requests only."""
    return bool(sr.is_campus)


async def standard_request(_session: AsyncSession, sr: Any, _actor: User) -> bool:
    """``send_invitation`` (booking link) is offered for non-campus requests only."""
    return not sr.is_campus
