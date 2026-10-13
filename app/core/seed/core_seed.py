"""Core seeder (order 0) — SPEC §11 (+ SPEC_RESOLUTIONS #6, #41–47).

Creates reference data, personas, articles + site settings, contacts, VIPs, the service
request / asset / lease return story (``story.py``), saved views, and recalculates stock.
Deterministic (``random.Random(42)``, fixed generation order) and relative to the business
date T. Idempotent: skips when sites already exist.
"""

from __future__ import annotations

from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import hash_password
from app.core.constants import BOOKING_SERVICE_ID
from app.core.enums import LocationType, SiteKind
from app.core.models import (
    Article,
    ArticleSiteSetting,
    DeviationCode,
    Region,
    RegionRule,
    SavedView,
    Site,
    StockLocation,
    User,
)
from app.core.region_resolver import normalize_pattern
from app.core.seed import story
from app.core.seed.framework import SeedContext
from app.core.seed.reference import (
    ARTICLES,
    COUNTRY,
    DEMO_PASSWORD,
    DEVIATION_CODES,
    HUB_LOCATION_TYPES,
    LOCATION_LABELS,
    PERSONAS,
    REGION_RULES,
    REGIONS,
    SITES,
    buffer_for,
    min_max_for,
)
from app.core.stock_math import recalculate

log = structlog.get_logger(__name__)


async def upsert_deviation_codes(session: AsyncSession) -> None:
    rows = [
        {
            "id": id_,
            "code": code,
            "label": label,
            "description": None,
            "category": category.value,
            "outcome": outcome.value,
            "requires_comment": requires,
            "is_active": True,
            "sort_order": id_,
        }
        for id_, code, label, category, outcome, requires in DEVIATION_CODES
    ]
    await session.execute(insert(DeviationCode).values(rows).on_conflict_do_nothing())


async def seed_reference(session: AsyncSession) -> None:
    sites: dict[str, Site] = {}
    for index, definition in enumerate(SITES):
        site = Site(
            code=definition.code,
            name=definition.name,
            kind=definition.kind,
            street=definition.street,
            zip=definition.zip,
            city=definition.city,
            country=COUNTRY,
            sort_order=index,
            parent_site_id=sites[definition.parent].id if definition.parent else None,
        )
        session.add(site)
        await session.flush()
        sites[definition.code] = site

    order = 0
    for definition in SITES:
        site = sites[definition.code]
        short = definition.city if definition.kind == SiteKind.HUB else definition.name
        types = HUB_LOCATION_TYPES if definition.kind == SiteKind.HUB else ("return",)
        for type_ in types:
            order += 1
            session.add(
                StockLocation(
                    code=f"{definition.code}-{type_.upper()}",
                    name=f"{short} · {LOCATION_LABELS[type_]}",
                    type=LocationType(type_),
                    site_id=site.id,
                    sort_order=order,
                )
            )
    for type_ in ("in_transit", "in_use", "off_fleet"):
        order += 1
        session.add(
            StockLocation(
                code=type_.upper(),
                name=LOCATION_LABELS[type_],
                type=LocationType(type_),
                site_id=None,
                sort_order=order,
            )
        )

    regions: dict[str, Region] = {}
    for index, definition in enumerate(REGIONS):
        pickup = definition.pickup
        region = Region(
            code=definition.code,
            name=definition.name,
            supplying_site_id=sites[definition.hub].id,
            default_delivery_type=definition.delivery,
            booking_destination_id=f"dest-{definition.code}",
            booking_service_id=BOOKING_SERVICE_ID,
            pickup_point_name=pickup[0] if pickup else None,
            pickup_point_street=pickup[1] if pickup else None,
            pickup_point_zip=pickup[2] if pickup else None,
            pickup_point_city=pickup[3] if pickup else None,
            sort_order=index,
        )
        session.add(region)
        regions[definition.code] = region
    await session.flush()

    for rule in REGION_RULES:
        session.add(
            RegionRule(
                region_id=regions[rule.region].id,
                match_kind=rule.kind,
                pattern=normalize_pattern(rule.kind, rule.pattern),
                procurement_type=rule.procurement,
                delivery_type=rule.delivery,
                is_campus=rule.campus,
                priority=rule.priority,
                note=rule.note,
            )
        )

    hubs = [s for s in sites.values() if s.kind == SiteKind.HUB]
    for number, category, name, model, device_type, active, kg, manufacturer in ARTICLES:
        article = Article(
            number=number,
            category=category,
            name=name,
            model_number=model,
            manufacturer=manufacturer,
            external_part_number=f"IL-{number[2:]}-{model.split('-')[0]}",
            internal_description=device_type,
            weight_kg=Decimal(kg),
            is_active=active,
        )
        session.add(article)
        await session.flush()
        min_qty, max_qty = min_max_for(category)
        for hub in hubs:
            session.add(
                ArticleSiteSetting(
                    article_id=article.id,
                    site_id=hub.id,
                    buffer_qty=buffer_for(category, hub.code),
                    min_qty=min_qty,
                    max_qty=max_qty,
                )
            )
    await session.flush()


async def seed_users(session: AsyncSession) -> None:
    password_hash = hash_password(DEMO_PASSWORD)  # one hash for all personas (demo only)
    sites = {s.code: s.id for s in (await session.scalars(select(Site))).all()}
    for persona in PERSONAS:
        session.add(
            User(
                email=persona.email,
                first_name=persona.first,
                last_name=persona.last,
                role=persona.role,
                home_site_id=sites[persona.home] if persona.home else None,
                password_hash=password_hash,
                is_demo_persona=True,
                persona_order=persona.order,
            )
        )
    await session.flush()


async def seed_saved_views(session: AsyncSession, ctx: SeedContext) -> None:
    dana = await ctx.user(session, "dana.dispatcher@example.com")
    eas = await ctx.site(session, "EAS")
    riv = await ctx.site(session, "RIV")
    monday = ctx.wd(0)
    week = [ctx.wd(n) for n in range(0, 5)]
    views = [
        ("Escalated – Eastgate supply", {"status": "escalated", "siteId": str(eas.id)}),
        (
            "Booked this week – Riverton",
            {
                "status": "appointment_booked",
                "siteId": str(riv.id),
                "appointmentFrom": monday.isoformat(),
                "appointmentTo": max(week).isoformat(),
            },
        ),
        ("Needs attention", {"needsAttention": True}),
    ]
    for order, (name, search) in enumerate(views):
        session.add(
            SavedView(
                user_id=dana.id,
                route="/service-requests",
                name=name,
                search=search,
                is_shared=True,
                sort_order=order,
            )
        )
    await session.flush()


async def seed_core(session: AsyncSession, ctx: SeedContext) -> None:
    await upsert_deviation_codes(session)
    if (await session.scalar(select(func.count()).select_from(Site))) or 0:
        log.info("core_seed_skipped", reason="sites exist")
        return
    await seed_reference(session)
    await seed_users(session)
    await story.seed_story(session, ctx)
    await seed_saved_views(session, ctx)
    await recalculate(session, ctx.now)
    log.info("core_seed_done", today=ctx.today.isoformat(), d0=ctx.d0.isoformat())
