"""Region resolver (SPEC §6.1, #40, #54) and stock math (SPEC §6.7, #18)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DeliveryType, ProcurementType, ServiceRequestStatus
from app.core.models import Article, ArticleSiteSetting, Site
from app.core.region_resolver import (
    apply_region,
    normalize_input,
    resolve_region,
    set_region_override,
)
from app.core.stock_math import above_max, below_min, order_qty
from app.testing import factories as f

P = ProcurementType


@pytest.mark.parametrize(
    ("zip_", "city", "cost_center", "procurement", "region", "campus", "delivery"),
    [
        ("10010", None, None, P.REPLACEMENT, "riverton", False, DeliveryType.TECHNICIAN),
        (
            "10500",
            "Riverton",
            None,
            P.REPLACEMENT,
            "riverton",
            True,
            DeliveryType.TECHNICIAN,
        ),  # campus zip
        (
            "40120",
            None,
            "CC-9100",
            P.NEW_HIRE,
            "riverton",
            True,
            DeliveryType.TECHNICIAN,
        ),  # cost centre wins
        ("50123", None, None, P.REPLACEMENT, "westbay", False, DeliveryType.PARCEL_TO_USER),
        ("50123", None, None, P.REMOVAL, "westbay", False, DeliveryType.TECHNICIAN),  # rule + #54
        (
            "60111",
            None,
            None,
            P.REMOVAL,
            "southmere",
            False,
            DeliveryType.TECHNICIAN,
        ),  # #54 everywhere
        (
            "40100",
            None,
            None,
            P.REPLACEMENT,
            "northport",
            False,
            DeliveryType.PARCEL_TO_PICKUP_POINT,
        ),
        (None, "  hILL   ford ", None, P.REPLACEMENT, None, False, None),
        (
            None,
            "Hillford",
            None,
            P.REPLACEMENT,
            "hillford",
            False,
            DeliveryType.TECHNICIAN,
        ),  # city rule
        ("9999", None, None, P.REPLACEMENT, None, False, None),  # 4 digits -> 09999, no rule
        ("99123", "Greyhaven", None, P.REPLACEMENT, None, False, None),
    ],
)
async def test_resolve_region(
    db_session: AsyncSession,
    zip_: str | None,
    city: str | None,
    cost_center: str | None,
    procurement: ProcurementType,
    region: str | None,
    campus: bool,
    delivery: DeliveryType | None,
) -> None:
    result = await resolve_region(
        db_session, zip=zip_, city=city, cost_center=cost_center, procurement_type=procurement
    )
    if region is None:
        assert result is None
        return
    assert result is not None
    assert result.region.code == region
    assert result.is_campus is campus
    assert result.delivery_type == delivery


def test_normalization() -> None:
    norm = normalize_input("1050", "  River   TON ", " cc-9100 ")
    assert (norm.zip, norm.city, norm.cost_center) == ("01050", "river ton", "CC-9100")
    assert normalize_input("ABC", None, None).zip is None


async def test_apply_region_and_override(db_session: AsyncSession) -> None:
    sr = await f.make_sr(
        db_session, region_code=None, delivery_zip="20111", delivery_city="Hillford"
    )
    sr.delivery_zip, sr.delivery_city = "20111", "Hillford"
    assert await apply_region(db_session, sr) is True
    assert sr.region is not None
    assert sr.region.code == "hillford"
    westbay = await f.region(db_session, "westbay")
    await set_region_override(db_session, sr, westbay)
    assert (sr.region_overridden, sr.region_rule_id, sr.delivery_type) == (
        True,
        None,
        DeliveryType.PARCEL_TO_USER,
    )
    assert sr.supplying_site_id == westbay.supplying_site_id
    assert await apply_region(db_session, sr) is False  # overridden SRs are skipped
    await set_region_override(db_session, sr, None)
    assert sr.region is not None
    assert sr.region.code == "hillford"
    sr.status = ServiceRequestStatus.APPOINTMENT_BOOKED
    sr.delivery_zip = "30111"
    assert await apply_region(db_session, sr) is False  # not region-mutable any more (#29)


def test_order_qty_formula() -> None:
    assert order_qty(demand=9, stock=3, buffer=4) == 10  # SPEC_RESOLUTIONS #18
    assert order_qty(demand=2, stock=10, buffer=4) == 0
    assert order_qty(demand=5, stock=5, buffer=2) == 2
    assert below_min(stock=2, quantity=1, min_qty=5) is True
    assert above_max(stock=30, quantity=15, max_qty=40) is True
    assert below_min(stock=2, quantity=1, min_qty=None) is False


async def _cell(session: AsyncSession, device_type: str, site_code: str) -> ArticleSiteSetting:
    stmt = (
        select(ArticleSiteSetting)
        .join(Article, Article.id == ArticleSiteSetting.article_id)
        .join(Site, Site.id == ArticleSiteSetting.site_id)
        .where(
            Article.internal_description == device_type, Article.is_active, Site.code == site_code
        )
    )
    return (await session.scalars(stmt)).one()


async def test_seeded_stock_story(db_session: AsyncSession) -> None:
    """SPEC §11.4: Hillford short on Laptop Performance 16, Eastgate on Monitor 34 Ultrawide,
    one cell above max (Riverton Dock U4)."""
    hil = await _cell(db_session, "Laptop Performance 16", "HIL")
    assert (hil.demand_qty, hil.stock_qty) == (9, 3)
    eas = await _cell(db_session, "Monitor 34 Ultrawide", "EAS")
    assert (eas.demand_qty, eas.stock_qty) == (6, 0)
    tb = await _cell(db_session, "Docking Station Thunderbolt", "EAS")
    assert order_qty(tb.demand_qty, tb.stock_qty, tb.buffer_qty) > 0
    dock = await _cell(db_session, "Docking Station USB-C", "RIV")
    assert dock.max_qty is not None
    assert dock.stock_qty > dock.max_qty
