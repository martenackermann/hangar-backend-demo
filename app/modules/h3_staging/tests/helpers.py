"""Shared helpers for the H3 tests (plain functions; fixtures live in conftest.py)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AssetStatus, DeliveryType, ServiceRequestStatus
from app.core.models import ServiceRequest
from app.core.timeutil import now_utc
from app.testing import factories as f

SR = ServiceRequestStatus
DANA = "dana.dispatcher@example.com"
THEO = "theo.technician@example.com"
VIC = "vic.viewer@example.com"
ADA = "ada.admin@example.com"
API = "/api/v1"


def soon(days: int = 2) -> Any:
    return now_utc() + timedelta(days=days)


async def assign_all(session: AsyncSession, sr: ServiceRequest) -> None:
    """Attach freshly made ``assigned`` assets to every new item (staging of the hub)."""
    for item in sr.new_items:
        asset = await f.make_asset(
            session,
            status=AssetStatus.ASSIGNED,
            device_type=item.requested_type or "Laptop Standard 14",
            location_code=f"{sr.supplying_site.code if sr.supplying_site else 'RIV'}-STAGING",
            sr=sr,
        )
        item.asset = asset
        item.asset_id = asset.id
        item.assigned_at = now_utc()
    await session.flush()


async def prepared_sr(
    session: AsyncSession,
    *,
    region_code: str = "riverton",
    delivery_type: DeliveryType | None = None,
    new_types: tuple[str, ...] = ("Laptop Standard 14",),
    old_tags: tuple[str, ...] = (),
    packed: bool = False,
    **kwargs: Any,
) -> ServiceRequest:
    """``ready_for_picking`` SR whose new assets are prepared at the hub's goods-out."""
    sr = await f.make_sr(
        session,
        status=SR.READY_FOR_PICKING,
        region_code=region_code,
        delivery_type=delivery_type,
        new_types=new_types,
        old_tags=old_tags,
        appointment=kwargs.pop("appointment", soon(1)),
        **kwargs,
    )
    hub = sr.supplying_site.code if sr.supplying_site else "RIV"
    for item in sr.new_items:
        asset = await f.make_asset(
            session,
            status=AssetStatus.PREPARED,
            device_type=item.requested_type or "Laptop Standard 14",
            location_code=f"{hub}-GOODS_OUT",
            sr=sr,
        )
        item.asset = asset
        item.asset_id = asset.id
        item.assigned_at = now_utc()
        if packed:
            item.packed_at = now_utc()
    await session.flush()
    return sr
