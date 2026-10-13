"""H3 test fixtures: SRs at each staging step with real stock to scan."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AssetStatus
from app.core.models import Asset, ServiceRequest, User
from app.core.slices import SliceRegistry
from app.core.timeutil import now_utc
from app.modules.h3_staging import register_actions
from app.modules.h3_staging.tests.helpers import DANA, SR, soon
from app.testing import factories as f

type Headers = Callable[[str], Awaitable[dict[str, str]]]


@pytest.fixture(autouse=True)
def h3_actions() -> None:
    """Core's action tests re-register (and then drop) some of our action names under their
    own owner; make sure ours are in place for every H3 test."""
    register_actions(SliceRegistry(owner="h3"))


@pytest.fixture
def make_staging_sr(db_session: AsyncSession) -> Callable[..., Awaitable[ServiceRequest]]:
    """SR in ``status`` (default in progress, Dana editing) with an appointment."""

    async def make(
        status: SR = SR.PERSONALIZATION_IN_PROGRESS,
        *,
        editor: User | str | None = "dana",
        **kwargs: Any,
    ) -> ServiceRequest:
        if editor == "dana":
            editor = await f.user(db_session, DANA)
        kwargs.setdefault("appointment", soon())
        return await f.make_sr(
            db_session,
            status=status,
            editor=editor if isinstance(editor, User) else None,
            claimed_at=now_utc() if editor else None,
            **kwargs,
        )

    return make


@pytest.fixture
def stock(db_session: AsyncSession) -> Callable[..., Awaitable[Asset]]:
    """An available asset on an installation place at a hub's staging location."""

    async def make(
        device_type: str = "Laptop Standard 14",
        *,
        location_code: str = "RIV-STAGING",
        place: str | None = "Z9-01",
        age_days: int = 10,
        status: AssetStatus = AssetStatus.AVAILABLE,
    ) -> Asset:
        asset = await f.make_asset(
            db_session,
            status=status,
            location_code=location_code,
            device_type=device_type,
            place=place,
        )
        asset.checked_in_at = now_utc() - timedelta(days=age_days)
        await db_session.flush()
        return asset

    return make
