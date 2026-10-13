"""Region resolution — SPEC §6.1 (+ SPEC_RESOLUTIONS #40, #54).

``resolve_region`` ranks matching ``region_rules``; ``apply_region`` writes the result to a
service request (skipped for manual overrides and for SRs past ``REGION_MUTABLE_STATUSES``
unless ``force``); ``set_region_override`` applies a dispatcher's manual choice;
``apply_lease_campus`` sets ``is_campus`` on lease returns.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    REGION_MUTABLE_STATUSES,
    DeliveryType,
    ProcurementType,
    RegionRuleMatchKind,
)
from app.core.models import LeaseReturn, Region, RegionRule, ServiceRequest
from app.core.rules import normalize_city, normalize_cost_center, normalize_zip

_KIND_RANK = {
    RegionRuleMatchKind.COST_CENTER: 4,
    RegionRuleMatchKind.ZIP: 3,
    RegionRuleMatchKind.ZIP_PREFIX: 2,
    RegionRuleMatchKind.CITY: 1,
}


@dataclass(frozen=True, slots=True)
class NormalizedInput:
    zip: str | None
    city: str | None
    cost_center: str | None


@dataclass(frozen=True, slots=True)
class RegionResolution:
    region_id: int
    region_rule_id: int
    supplying_site_id: int
    delivery_type: DeliveryType
    is_campus: bool
    region: Region
    rule: RegionRule


def normalize_input(zip: str | None, city: str | None, cost_center: str | None) -> NormalizedInput:
    return NormalizedInput(
        normalize_zip(zip), normalize_city(city), normalize_cost_center(cost_center)
    )


def normalize_pattern(kind: RegionRuleMatchKind, pattern: str) -> str:
    """Normalize a rule pattern on save (zip digits, city case-folded, cost centre upper)."""
    if kind in (RegionRuleMatchKind.ZIP, RegionRuleMatchKind.ZIP_PREFIX):
        return "".join(ch for ch in pattern if ch.isdigit())
    if kind == RegionRuleMatchKind.CITY:
        return normalize_city(pattern) or ""
    return normalize_cost_center(pattern) or ""


def rank_key(rule: RegionRule) -> tuple[int, int, int, int, int]:
    """Sort key (ascending = better): specific procurement, kind rank, prefix length, priority, id."""
    return (
        0 if rule.procurement_type is not None else 1,
        -_KIND_RANK[rule.match_kind],
        -len(rule.pattern) if rule.match_kind == RegionRuleMatchKind.ZIP_PREFIX else 0,
        -rule.priority,
        rule.id,
    )


async def candidate_rules(
    session: AsyncSession,
    *,
    zip: str | None,
    city: str | None,
    cost_center: str | None,
    procurement_type: ProcurementType | None,
) -> tuple[NormalizedInput, list[RegionRule]]:
    """All matching active rules of active regions, best first."""
    norm = normalize_input(zip, city, cost_center)
    clauses = []
    if norm.cost_center:
        clauses.append(
            (RegionRule.match_kind == RegionRuleMatchKind.COST_CENTER)
            & (RegionRule.pattern == norm.cost_center)
        )
    if norm.zip:
        clauses.append(
            (RegionRule.match_kind == RegionRuleMatchKind.ZIP) & (RegionRule.pattern == norm.zip)
        )
        # prefix rules are few; the "zip starts with pattern" test is done below
        clauses.append(RegionRule.match_kind == RegionRuleMatchKind.ZIP_PREFIX)
    if norm.city:
        clauses.append(
            (RegionRule.match_kind == RegionRuleMatchKind.CITY) & (RegionRule.pattern == norm.city)
        )
    if not clauses:
        return norm, []
    stmt = (
        select(RegionRule)
        .join(Region, RegionRule.region_id == Region.id)
        .where(RegionRule.is_active, Region.is_active)
        .where(or_(*clauses))
        .where(
            or_(
                RegionRule.procurement_type.is_(None),
                RegionRule.procurement_type == procurement_type,
            )
            if procurement_type is not None
            else RegionRule.procurement_type.is_(None)
        )
    )
    rules = list((await session.scalars(stmt)).all())
    matched = [
        r
        for r in rules
        if r.match_kind != RegionRuleMatchKind.ZIP_PREFIX
        or (norm.zip and norm.zip.startswith(r.pattern))
    ]
    matched.sort(key=rank_key)
    return norm, matched


def _delivery_for(
    rule: RegionRule | None,
    region: Region,
    procurement_type: ProcurementType | None,
    is_campus: bool,
) -> DeliveryType:
    if is_campus or procurement_type == ProcurementType.REMOVAL:  # SPEC §6.1 step 4, #54
        return DeliveryType.TECHNICIAN
    if rule is not None and rule.delivery_type is not None:
        return rule.delivery_type
    return region.default_delivery_type


async def resolve_region(
    session: AsyncSession,
    *,
    zip: str | None,
    city: str | None,
    cost_center: str | None,
    procurement_type: ProcurementType | None,
) -> RegionResolution | None:
    _, rules = await candidate_rules(
        session, zip=zip, city=city, cost_center=cost_center, procurement_type=procurement_type
    )
    if not rules:
        return None
    rule = rules[0]
    region = rule.region
    return RegionResolution(
        region_id=region.id,
        region_rule_id=rule.id,
        supplying_site_id=region.supplying_site_id,
        delivery_type=_delivery_for(rule, region, procurement_type, rule.is_campus),
        is_campus=rule.is_campus,
        region=region,
        rule=rule,
    )


async def apply_region(session: AsyncSession, sr: ServiceRequest, *, force: bool = False) -> bool:
    """Re-resolve and write ``region_*``, ``supplying_site_id``, ``delivery_type``,
    ``is_campus``. Returns ``True`` if anything changed. Never changes status."""
    if sr.region_overridden:
        return False
    if not force and sr.status is not None and sr.status not in REGION_MUTABLE_STATUSES:
        return False
    resolution = await resolve_region(
        session,
        zip=sr.delivery_zip,
        city=sr.delivery_city,
        cost_center=sr.cost_center,
        procurement_type=sr.procurement_type,
    )
    before = (sr.region_id, sr.region_rule_id, sr.supplying_site_id, sr.delivery_type, sr.is_campus)
    if resolution is None:
        sr.region, sr.region_rule, sr.supplying_site = None, None, None
        sr.region_id = sr.region_rule_id = sr.supplying_site_id = None
        sr.delivery_type, sr.is_campus = None, False
    else:
        sr.region = resolution.region
        sr.region_rule = resolution.rule
        sr.supplying_site = resolution.region.supplying_site
        sr.region_id = resolution.region_id
        sr.region_rule_id = resolution.region_rule_id
        sr.supplying_site_id = resolution.supplying_site_id
        sr.delivery_type = resolution.delivery_type
        sr.is_campus = resolution.is_campus
    after = (sr.region_id, sr.region_rule_id, sr.supplying_site_id, sr.delivery_type, sr.is_campus)
    return before != after


async def set_region_override(
    session: AsyncSession, sr: ServiceRequest, region: Region | None
) -> None:
    """Dispatcher override (SPEC_RESOLUTIONS #40); ``None`` clears it and re-resolves."""
    if region is None:
        sr.region_overridden = False
        await apply_region(session, sr, force=True)
        return
    sr.region_overridden = True
    sr.region = region
    sr.region_id = region.id
    sr.region_rule = None
    sr.region_rule_id = None
    sr.supplying_site = region.supplying_site
    sr.supplying_site_id = region.supplying_site_id
    sr.is_campus = False  # regions carry no campus flag; campus is a rule property
    sr.delivery_type = _delivery_for(None, region, sr.procurement_type, False)


async def apply_lease_campus(session: AsyncSession, lease: LeaseReturn) -> bool:
    """Lease returns use the resolver only for ``is_campus`` (contact address + cost centre)."""
    contact = lease.contact
    resolution = await resolve_region(
        session,
        zip=contact.zip if contact else None,
        city=contact.city if contact else None,
        cost_center=lease.cost_center,
        procurement_type=None,
    )
    is_campus = bool(resolution and resolution.is_campus)
    changed = lease.is_campus != is_campus
    lease.is_campus = is_campus
    return changed
