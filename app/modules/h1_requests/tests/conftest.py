"""H1 test fixtures.

Core tests register throw-away actions under the same names (``send_invitation``,
``cancel``, ``pause_communication`` …) and unregister them afterwards, which also drops
ours from the shared ActionRegistry. Re-register the H1 actions/authorizers before every
H1 test so the module order does not matter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import LeaseReturn
from app.core.slices import SliceRegistry
from app.core.statemachine import authorizers
from app.modules.h1_requests import register_actions, register_authorizers
from app.modules.h1_requests.models import DispatchSettings
from app.testing import factories as f

DANA = "dana.dispatcher@example.com"
THEO = "theo.technician@example.com"
VIC = "vic.viewer@example.com"
ADA = "ada.admin@example.com"

type Headers = Callable[[str], Awaitable[dict[str, str]]]


@pytest.fixture(autouse=True)
def h1_registered() -> None:
    registry = SliceRegistry(owner="h1")
    authorizers.remove_owner("h1")
    register_actions(registry)
    register_authorizers(registry)


@pytest.fixture
async def settings_row(db_session: AsyncSession) -> DispatchSettings:
    row = await db_session.get(DispatchSettings, 1)
    assert row is not None
    return row


async def make_lease(
    session: AsyncSession, *, retirement_date: date | None = None, **fields: Any
) -> LeaseReturn:
    """``factories.make_lease`` with an overridable retirement date."""
    lease = await f.make_lease(session, **fields)
    if retirement_date is not None:
        lease.retirement_date = retirement_date
        await session.flush()
    return lease
