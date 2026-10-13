"""Reference/master data response models + builders (API.md §3.4; SPEC_RESOLUTIONS #22)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    SR_NON_TERMINAL,
    DeliveryType,
    DeviationCategory,
    DeviationOutcome,
    Role,
    SiteKind,
)
from app.core.models import (
    DeviationCode,
    Region,
    RegionRule,
    ServiceRequest,
    Site,
    StockLocation,
    User,
)
from app.core.read_models.refs import location_ref, site_ref
from app.core.schemas import Address, ApiModel, LocationRef, SiteRef


class SiteOut(ApiModel):
    id: int
    code: str
    name: str
    kind: SiteKind
    parent_site_id: int | None
    address: Address
    is_active: bool
    sort_order: int
    locations: list[LocationRef]


class PickupPoint(ApiModel):
    name: str
    street: str | None
    zip: str | None
    city: str | None


class RegionOut(ApiModel):
    id: int
    code: str
    name: str
    supplying_site: SiteRef
    default_delivery_type: DeliveryType
    booking_destination_id: str | None
    booking_service_id: str | None
    pickup_point: PickupPoint | None
    is_active: bool
    sort_order: int
    rule_count: int
    open_service_requests: int


class StockLocationOut(LocationRef):
    is_active: bool
    sort_order: int
    site: SiteRef | None


class UserSummary(ApiModel):
    id: int
    email: str
    display_name: str
    first_name: str
    last_name: str
    role: Role
    home_site: SiteRef | None
    is_active: bool
    last_login_at: datetime | None
    is_demo_persona: bool


class DeviationCodeOut(ApiModel):
    id: int
    code: str
    label: str
    description: str | None
    category: DeviationCategory
    outcome: DeviationOutcome
    requires_comment: bool
    is_active: bool
    sort_order: int


def build_site(site: Site) -> SiteOut:
    return SiteOut(
        id=site.id,
        code=site.code,
        name=site.name,
        kind=site.kind,
        parent_site_id=site.parent_site_id,
        address=Address(street=site.street, zip=site.zip, city=site.city, country=site.country),
        is_active=site.is_active,
        sort_order=site.sort_order,
        locations=[ref for ref in (location_ref(loc) for loc in site.locations) if ref is not None],
    )


async def region_counts(
    session: AsyncSession, region_ids: Sequence[int]
) -> tuple[dict[int, int], dict[int, int]]:
    if not region_ids:
        return {}, {}
    rules = await session.execute(
        select(RegionRule.region_id, func.count())
        .where(RegionRule.region_id.in_(region_ids))
        .group_by(RegionRule.region_id)
    )
    srs = await session.execute(
        select(ServiceRequest.region_id, func.count())
        .where(ServiceRequest.region_id.in_(region_ids), ServiceRequest.status.in_(SR_NON_TERMINAL))
        .group_by(ServiceRequest.region_id)
    )
    return {r: int(n) for r, n in rules.all()}, {r: int(n) for r, n in srs.all() if r is not None}


def build_region(region: Region, rule_count: int, open_srs: int) -> RegionOut:
    supplying = site_ref(region.supplying_site)
    assert supplying is not None
    pickup = (
        PickupPoint(
            name=region.pickup_point_name,
            street=region.pickup_point_street,
            zip=region.pickup_point_zip,
            city=region.pickup_point_city,
        )
        if region.pickup_point_name
        else None
    )
    return RegionOut(
        id=region.id,
        code=region.code,
        name=region.name,
        supplying_site=supplying,
        default_delivery_type=region.default_delivery_type,
        booking_destination_id=region.booking_destination_id,
        booking_service_id=region.booking_service_id,
        pickup_point=pickup,
        is_active=region.is_active,
        sort_order=region.sort_order,
        rule_count=rule_count,
        open_service_requests=open_srs,
    )


async def build_regions(session: AsyncSession, regions: Sequence[Region]) -> list[RegionOut]:
    rules, srs = await region_counts(session, [r.id for r in regions])
    return [build_region(r, rules.get(r.id, 0), srs.get(r.id, 0)) for r in regions]


def build_stock_location(location: StockLocation) -> StockLocationOut:
    return StockLocationOut(
        id=location.id,
        code=location.code,
        name=location.name,
        type=location.type,
        site_id=location.site_id,
        is_active=location.is_active,
        sort_order=location.sort_order,
        site=site_ref(location.site),
    )


def build_user_summary(user: User) -> UserSummary:
    return UserSummary(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        first_name=user.first_name,
        last_name=user.last_name,
        role=user.role,
        home_site=site_ref(user.home_site),
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        is_demo_persona=user.is_demo_persona,
    )


def build_deviation_code(code: DeviationCode) -> DeviationCodeOut:
    return DeviationCodeOut.model_validate(code)
