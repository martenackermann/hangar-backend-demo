"""Regions, region rules, resolver tester and reapply (API.md §5.3; SPEC §6.1; RES #29)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gateways
from app.core.enums import (
    REGION_MUTABLE_STATUSES,
    SR_NON_TERMINAL,
    DeliveryType,
    LeaseReturnStatus,
    RegionRuleMatchKind,
    SiteKind,
)
from app.core.errors import ConflictError, NotFoundError, ValidationProblem
from app.core.models import LeaseReturn, Region, RegionRule, ServiceRequest, Site
from app.core.read_models.master import build_regions
from app.core.read_models.refs import region_ref, site_ref
from app.core.region_resolver import (
    apply_lease_campus,
    apply_region,
    candidate_rules,
    normalize_pattern,
    resolve_region,
)

from ..schemas import (
    NormalizedInputOut,
    RankedRule,
    RegionDetail,
    RegionPatch,
    RegionResolutionResult,
    RegionRuleInput,
    RegionRuleOut,
    RegionRulePatch,
    RegionTestIn,
)

PICKUP = DeliveryType.PARCEL_TO_PICKUP_POINT
EXAMPLE_REFERENCE = "SR0012345"


# --- read models ---------------------------------------------------------------------------


async def rule_match_counts(session: AsyncSession, ids: Sequence[int]) -> dict[int, int]:
    if not ids:
        return {}
    rows = await session.execute(
        select(ServiceRequest.region_rule_id, func.count())
        .where(
            ServiceRequest.region_rule_id.in_(ids),
            ServiceRequest.status.in_(SR_NON_TERMINAL),
        )
        .group_by(ServiceRequest.region_rule_id)
    )
    return {r: int(n) for r, n in rows.all() if r is not None}


def build_rule(rule: RegionRule, match_count: int) -> RegionRuleOut:
    region = region_ref(rule.region)
    assert region is not None
    return RegionRuleOut(
        id=rule.id,
        region_id=rule.region_id,
        match_kind=rule.match_kind,
        pattern=rule.pattern,
        procurement_type=rule.procurement_type,
        delivery_type=rule.delivery_type,
        is_campus=rule.is_campus,
        priority=rule.priority,
        is_active=rule.is_active,
        note=rule.note,
        normalized_pattern=rule.pattern,
        region=region,
        match_count=match_count,
    )


async def build_rules(session: AsyncSession, rules: Sequence[RegionRule]) -> list[RegionRuleOut]:
    counts = await rule_match_counts(session, [r.id for r in rules])
    return [build_rule(r, counts.get(r.id, 0)) for r in rules]


async def rules_of(session: AsyncSession, region_id: int) -> list[RegionRule]:
    return list(
        (
            await session.scalars(
                select(RegionRule)
                .where(RegionRule.region_id == region_id)
                .order_by(RegionRule.match_kind, RegionRule.pattern, RegionRule.id)
            )
        ).all()
    )


def booking_link_example(region: Region) -> str | None:
    if not region.booking_destination_id or not region.booking_service_id:
        return None
    return gateways.booking().build_booking_link(
        destination_id=region.booking_destination_id,
        service_id=region.booking_service_id,
        reference=EXAMPLE_REFERENCE,
    )


async def build_region_detail(session: AsyncSession, region: Region) -> RegionDetail:
    await session.flush()
    await session.refresh(region)
    (base,) = await build_regions(session, [region])
    return RegionDetail(
        **base.model_dump(by_alias=False),
        rules=await build_rules(session, await rules_of(session, region.id)),
        booking_link_example=booking_link_example(region),
    )


async def get_region(session: AsyncSession, region_id: int) -> Region:
    region = await session.get(Region, region_id)
    if region is None:
        raise NotFoundError(f"Region {region_id} not found.")
    return region


async def get_rule(session: AsyncSession, rule_id: int) -> RegionRule:
    rule = await session.get(RegionRule, rule_id)
    if rule is None:
        raise NotFoundError(f"Region rule {rule_id} not found.")
    return rule


# --- region PATCH --------------------------------------------------------------------------


async def update_region(session: AsyncSession, region: Region, body: RegionPatch) -> Region:
    values = body.model_dump(exclude_unset=True, by_alias=False)
    errors: dict[str, list[str]] = {}
    for name in ("name", "supplying_site_id", "default_delivery_type", "is_active", "sort_order"):
        if name in values and values[name] is None:
            errors[_camel(name)] = ["This field cannot be empty."]
    if values.get("supplying_site_id") is not None:
        site = await session.get(Site, values["supplying_site_id"])
        if site is None or site.kind != SiteKind.HUB:
            errors["supplyingSiteId"] = ["Choose a hub as supplying site."]
    if errors:
        raise ValidationProblem(errors)
    if "pickup_point" in values:
        pickup: dict[str, Any] | None = values.pop("pickup_point")
        region.pickup_point_name = pickup["name"] if pickup else None
        region.pickup_point_street = pickup.get("street") if pickup else None
        region.pickup_point_zip = pickup.get("zip") if pickup else None
        region.pickup_point_city = pickup.get("city") if pickup else None
    for name, value in values.items():
        if name == "supplying_site_id":
            region.supplying_site = await session.get(Site, value)  # type: ignore[assignment]
        setattr(region, name, value)
    if not region.pickup_point_name:
        uses_pickup = region.default_delivery_type == PICKUP or (
            await session.scalar(
                select(func.count())
                .select_from(RegionRule)
                .where(
                    RegionRule.region_id == region.id,
                    RegionRule.is_active,
                    RegionRule.delivery_type == PICKUP,
                )
            )
        )
        if uses_pickup:
            raise ValidationProblem(
                {"pickupPoint": ["A pickup point is required for parcel-to-pickup-point delivery."]}
            )
    await session.flush()
    return region


# --- rules ---------------------------------------------------------------------------------


def _validate_pattern(kind: RegionRuleMatchKind, pattern: str) -> str:
    normalized = normalize_pattern(kind, pattern)
    if kind == RegionRuleMatchKind.ZIP and len(normalized) not in (4, 5):
        raise ValidationProblem({"pattern": ["A zip rule needs a 4–5 digit zip code."]})
    if kind == RegionRuleMatchKind.ZIP and len(normalized) == 4:
        normalized = normalized.zfill(5)
    if kind == RegionRuleMatchKind.ZIP_PREFIX and not 1 <= len(normalized) <= 5:
        raise ValidationProblem({"pattern": ["A zip prefix needs 1–5 digits."]})
    if not normalized:
        raise ValidationProblem({"pattern": ["The pattern is empty after normalization."]})
    return normalized


async def _save_rule(session: AsyncSession, rule: RegionRule, values: dict[str, Any]) -> RegionRule:
    kind = RegionRuleMatchKind(values.get("match_kind", rule.match_kind))
    pattern = _validate_pattern(kind, values.get("pattern", rule.pattern))
    region_id = values.get("region_id", rule.region_id)
    region = await session.get(Region, region_id) if region_id is not None else None
    if region is None:
        raise ValidationProblem({"regionId": ["Unknown region."]})
    delivery = values.get("delivery_type", rule.delivery_type)
    if delivery == PICKUP and not region.pickup_point_name:
        raise ValidationProblem(
            {"deliveryType": [f"Region {region.name} has no pickup point. Add one first."]}
        )
    procurement = values.get("procurement_type", rule.procurement_type)
    duplicate = await session.scalar(
        select(RegionRule.id).where(
            RegionRule.match_kind == kind,
            RegionRule.pattern == pattern,
            RegionRule.procurement_type.is_(None)
            if procurement is None
            else RegionRule.procurement_type == procurement,
            RegionRule.id != (rule.id or 0),
        )
    )
    if duplicate is not None:
        raise ConflictError(
            "A rule with the same match kind, pattern and procurement type already exists.",
            errors={"pattern": ["A rule for this pattern already exists."]},
        )
    for name, value in values.items():
        setattr(rule, name, value)
    rule.match_kind = kind
    rule.pattern = pattern
    rule.region = region
    rule.region_id = region.id
    if rule.id is None:
        session.add(rule)
    await session.flush()
    return rule


async def create_rule(session: AsyncSession, body: RegionRuleInput) -> RegionRule:
    return await _save_rule(
        session,
        RegionRule(),
        body.model_dump(by_alias=False),
    )


async def update_rule(session: AsyncSession, rule: RegionRule, body: RegionRulePatch) -> RegionRule:
    values = body.model_dump(exclude_unset=True, by_alias=False)
    nulls = [
        k
        for k in ("region_id", "match_kind", "pattern", "is_campus", "priority", "is_active")
        if k in values and values[k] is None
    ]
    if nulls:
        raise ValidationProblem({_camel(k): ["This field cannot be empty."] for k in nulls})
    return await _save_rule(session, rule, values)


async def resolve_for_test(session: AsyncSession, body: RegionTestIn) -> RegionResolutionResult:
    norm, rules = await candidate_rules(
        session,
        zip=body.zip,
        city=body.city,
        cost_center=body.cost_center,
        procurement_type=body.procurement_type,
    )
    built = await build_rules(session, rules)
    resolution = await resolve_region(
        session,
        zip=body.zip,
        city=body.city,
        cost_center=body.cost_center,
        procurement_type=body.procurement_type,
    )
    normalized = NormalizedInputOut(zip=norm.zip, city=norm.city, cost_center=norm.cost_center)
    candidates = [RankedRule(rule=rule, rank=i + 1) for i, rule in enumerate(built)]
    if resolution is None:
        return RegionResolutionResult(
            resolved=False,
            region=None,
            rule=None,
            delivery_type=None,
            is_campus=False,
            supplying_site=None,
            normalized_input=normalized,
            candidates=candidates,
        )
    return RegionResolutionResult(
        resolved=True,
        region=region_ref(resolution.region),
        rule=built[0] if built else None,
        delivery_type=resolution.delivery_type,
        is_campus=resolution.is_campus,
        supplying_site=site_ref(resolution.region.supplying_site),
        normalized_input=normalized,
        candidates=candidates,
    )


# --- reapply -------------------------------------------------------------------------------


async def reapply_regions(session: AsyncSession, progress: Any = None) -> dict[str, Any]:
    """Re-resolve SRs in ``REGION_MUTABLE_STATUSES`` (RES #29) and lease ``is_campus``."""
    srs = (
        await session.scalars(
            select(ServiceRequest)
            .where(
                ServiceRequest.status.in_(REGION_MUTABLE_STATUSES),
                ServiceRequest.region_overridden.is_(False),
            )
            .order_by(ServiceRequest.id)
        )
    ).all()
    leases = (
        await session.scalars(
            select(LeaseReturn)
            .where(LeaseReturn.status != LeaseReturnStatus.CLOSED)
            .order_by(LeaseReturn.id)
        )
    ).all()
    total = len(srs) + len(leases)
    sr_changed = lease_changed = unresolved = done = 0
    for sr in srs:
        if await apply_region(session, sr):
            sr_changed += 1
        if sr.region_id is None:
            unresolved += 1
        done += 1
        if progress is not None and done % 100 == 0:
            await progress(done, total, f"Re-resolved {done} of {total}")
    for lease in leases:
        if await apply_lease_campus(session, lease):
            lease_changed += 1
        done += 1
        if progress is not None and done % 100 == 0:
            await progress(done, total, f"Re-resolved {done} of {total}")
    await session.flush()
    if progress is not None:
        await progress(total, total, "Done")
    return {
        "kind": "regions_reapply",
        "serviceRequestsChanged": sr_changed,
        "leaseReturnsChanged": lease_changed,
        "unresolved": unresolved,
    }


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)
