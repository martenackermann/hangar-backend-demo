"""H5 seeder (order 50) — SPEC §11.7 (+ SPEC_RESOLUTIONS #41–47).

Consistent with the core story, which it reads from core tables (never invents statuses):

- **Inventories** with the fixed ids of ``SEED_INVENTORY_IDS``: Eastgate closed T−60 and
  Riverton closed T−30 (corrections applied; the missing devices are exactly the assets the
  core marked missing with ``source_ref = inventory:{id}``), Hillford open since wd(−1)
  09:00 (≈ 60 % of the expected set scanned by Tara, 3 wrong-location, 2 unknown serials).
  Historic expected sets are rebuilt from ``asset_movements`` at the opening time.
- **Call-off orders**: per hub sent at T−28, T−14, T−7 (documents stored) + one Eastgate
  draft created today from the current matrix.
- **Deactivation batches** 1 (T−20, returned to lessor, 30 deactivated + 2 not found) and
  2 (T−6, bought out, 6 deactivated) matching the core movements ``deactivation:{id}``.
- Finally a stock recalculation.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.constants import SEED_INVENTORY_IDS
from app.core.enums import (
    DEACTIVATED,
    AssetAction,
    AssetStatus,
    CallOffOrderStatus,
    DeactivationReason,
    InventoryScanResult,
    InventoryStatus,
    LeaseReturnStatus,
    LocationType,
    SiteKind,
)
from app.core.files import store_file
from app.core.models import (
    Article,
    ArticleSiteSetting,
    Asset,
    AssetMovement,
    LeaseReturn,
    Site,
    StockLocation,
    User,
)
from app.core.seed.framework import SeedContext
from app.core.stock_math import order_qty, recalculate
from app.core.timeutil import add_working_days

from .models import CallOffOrder, CallOffOrderLine, DeactivationBatch, Inventory, InventoryScan
from .services.calloff import document_for, line_from_setting
from .services.common import csv_bytes
from .services.deactivation import SAMPLE_SIZE, Outcome, Tally, finish_batch
from .services.documents import PDF_TYPE, XLSX_TYPE, order_pdf, order_xlsx
from .services.inventory import (
    EXPECTED_TYPES,
    Correction,
    Entry,
    _frozen,
    live_entries,
    store_reports,
)

log = structlog.get_logger(__name__)

DANA = "dana.dispatcher@example.com"
THEO = "theo.technician@example.com"
TARA = "tara.lindqvist@example.com"
OMAR = "omar.reyes@example.com"
BASE32 = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


async def seed_stock(session: AsyncSession, ctx: SeedContext) -> None:
    if (await session.scalar(select(func.count()).select_from(Inventory))) or 0:
        log.info("h5_seed_skipped", reason="inventories exist")
        return
    if not (await session.scalar(select(func.count()).select_from(Site))):
        return  # no core story (should not happen: core seeds first)
    await seed_inventories(session, ctx)
    await seed_calloff_orders(session, ctx)
    await seed_deactivations(session, ctx)
    await recalculate(session, ctx.now)
    log.info("h5_seed_done")


# --- inventories ------------------------------------------------------------------------------------


async def _historic_expected(
    session: AsyncSession, site: Site, at: datetime
) -> list[tuple[Asset, int, AssetStatus]]:
    """Assets located at ``site`` (expected types) at time ``at``, from the movement ledger."""
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT ON (m.asset_id) m.asset_id, m.to_location_id, m.to_status
                FROM asset_movements m
                WHERE m.occurred_at <= :at
                ORDER BY m.asset_id, m.occurred_at DESC, m.id DESC
                """
            ),
            {"at": at},
        )
    ).all()
    locations = {
        loc.id
        for loc in (
            await session.scalars(
                select(StockLocation).where(
                    StockLocation.site_id == site.id, StockLocation.type.in_(EXPECTED_TYPES)
                )
            )
        ).all()
    }
    wanted = {
        r.asset_id: (r.to_location_id, AssetStatus(r.to_status))
        for r in rows
        if r.to_location_id in locations and AssetStatus(r.to_status) not in DEACTIVATED
    }
    if not wanted:
        return []
    assets = (await session.scalars(select(Asset).where(Asset.id.in_(list(wanted))))).all()
    return sorted(
        ((a, wanted[a.id][0], wanted[a.id][1]) for a in assets), key=lambda t: t[0].serial_number
    )


def _fake_serial(rng: random.Random, prefix: str = "KD") -> str:
    return prefix + "X" + "".join(rng.choice(BASE32) for _ in range(7))


async def _user(session: AsyncSession, ctx: SeedContext, email: str) -> User:
    return await ctx.user(session, email)


async def seed_inventories(session: AsyncSession, ctx: SeedContext) -> None:
    for code, days_ago, scanner in (("EAS", 60, OMAR), ("RIV", 30, THEO)):
        await _closed_inventory(session, ctx, code, days_ago, scanner)
    await _open_hillford(session, ctx)


async def _closed_inventory(
    session: AsyncSession, ctx: SeedContext, code: str, days_ago: int, scanner_email: str
) -> None:
    rng = ctx.rng
    site = await ctx.site(session, code)
    dana = await _user(session, ctx, DANA)
    scanner = await _user(session, ctx, scanner_email)
    count_day = ctx.today - timedelta(days=days_ago)
    opened_at = ctx.at(count_day - timedelta(days=2), 8)
    closed_at = ctx.at(count_day, 16)
    inventory_id = SEED_INVENTORY_IDS[code]
    missing_ids = set(
        (
            await session.scalars(
                select(AssetMovement.asset_id).where(
                    AssetMovement.source_ref == f"inventory:{inventory_id}",
                    AssetMovement.action == AssetAction.MARK_MISSING,
                )
            )
        ).all()
    )
    expected = await _historic_expected(session, site, opened_at)
    inventory = Inventory(
        site_id=site.id,
        site=site,
        status=InventoryStatus.OPEN,
        opened_at=opened_at,
        opened_by=dana.id,
        opener=dana,
        note=f"Quarterly stock-take {site.name}",
        created_at=opened_at,
    )
    session.add(inventory)
    await session.flush()
    if inventory.id != inventory_id:
        log.warning("h5_seed_inventory_id_mismatch", expected=inventory_id, got=inventory.id)
    locations = {loc.id: loc for loc in (await session.scalars(select(StockLocation))).all()}
    entries: list[Entry] = []
    span = (closed_at - timedelta(hours=1) - opened_at).total_seconds()
    scan_times = sorted(
        opened_at + timedelta(seconds=rng.uniform(0, span)) for _ in range(len(expected) + 1)
    )
    times = iter(scan_times)
    for asset, location_id, status in expected:
        location = locations[location_id]
        if asset.id in missing_ids:
            entries.append(
                Entry(
                    category="missing",
                    serial=asset.serial_number,
                    asset=asset,
                    status=status,
                    recorded_location=location,
                    scanned_location=None,
                    scanned_by=None,
                    scanned_at=None,
                    correction=Correction(AssetAction.MARK_MISSING),
                    corrected=True,
                )
            )
            continue
        at = next(times)
        session.add(
            InventoryScan(
                inventory_id=inventory.id,
                serial_number=asset.serial_number,
                asset_id=asset.id,
                result=InventoryScanResult.MATCHED,
                recorded_location_id=location.id,
                scanned_by=scanner.id,
                scanned_at=at,
            )
        )
        entries.append(
            Entry("matched", asset.serial_number, asset, status, location, None, scanner, at)
        )
    unknown_serial = _fake_serial(rng)
    at = next(times)
    session.add(
        InventoryScan(
            inventory_id=inventory.id,
            serial_number=unknown_serial,
            asset_id=None,
            result=InventoryScanResult.UNKNOWN,
            scanned_by=scanner.id,
            scanned_at=at,
        )
    )
    entries.append(Entry("unknown", unknown_serial, None, None, None, None, scanner, at))
    await session.flush()
    order = {"missing": 0, "wrong_location": 1, "unknown": 2, "matched": 3}
    entries.sort(key=lambda e: (order[e.category], e.serial))
    await store_reports(session, inventory, entries, dana)
    missing = sum(1 for e in entries if e.category == "missing")
    matched = sum(1 for e in entries if e.category == "matched")
    inventory.result_summary = {
        "expected": len(expected),
        "matched": matched,
        "missing": missing,
        "wrongLocation": 0,
        "unknown": 1,
        "corrected": missing,
    }
    inventory.reconciliation = [_frozen(e) for e in entries]
    inventory.status = InventoryStatus.CLOSED
    inventory.closed_at = closed_at
    inventory.closed_by = dana.id
    inventory.closer = dana
    inventory.count_date = count_day
    inventory.apply_corrections = True
    await session.flush()


async def _open_hillford(session: AsyncSession, ctx: SeedContext) -> None:
    rng = ctx.rng
    site = await ctx.site(session, "HIL")
    dana = await _user(session, ctx, DANA)
    tara = await _user(session, ctx, TARA)
    opened_at = ctx.at(ctx.wd(-1), 9)
    inventory = Inventory(
        site_id=site.id,
        site=site,
        status=InventoryStatus.OPEN,
        opened_at=opened_at,
        opened_by=dana.id,
        opener=dana,
        note="Stock-take before the next call-off",
        created_at=opened_at,
    )
    session.add(inventory)
    await session.flush()
    if inventory.id != SEED_INVENTORY_IDS["HIL"]:
        log.warning("h5_seed_inventory_id_mismatch", expected=SEED_INVENTORY_IDS["HIL"])
    entries = await live_entries(session, inventory)  # all expected assets are "missing" now
    expected = [e.asset for e in entries if e.asset is not None]
    scanned = [a for a in expected if rng.random() < 0.6]
    zones = {
        loc.type: loc
        for loc in (
            await session.scalars(select(StockLocation).where(StockLocation.site_id == site.id))
        ).all()
    }
    elsewhere = (
        await session.scalars(
            select(Asset)
            .join(StockLocation, StockLocation.id == Asset.location_id)
            .join(Site, Site.id == StockLocation.site_id)
            .where(
                Site.code.in_(("EAS", "RIV")),
                Asset.status == AssetStatus.AVAILABLE,
                StockLocation.type == LocationType.STAGING,
            )
            .order_by(Asset.id)
        )
    ).all()
    wrong = (
        [elsewhere[i] for i in sorted(rng.sample(range(len(elsewhere)), k=3))]
        if len(elsewhere) >= 3
        else []
    )
    end = ctx.now - timedelta(minutes=10)
    span = max(60.0, (end - (opened_at + timedelta(minutes=20))).total_seconds())
    total = len(scanned) + len(wrong) + 2
    times = iter(
        sorted(
            opened_at + timedelta(minutes=20, seconds=rng.uniform(0, span)) for _ in range(total)
        )
    )
    for asset in scanned:
        session.add(
            InventoryScan(
                inventory_id=inventory.id,
                serial_number=asset.serial_number,
                asset_id=asset.id,
                result=InventoryScanResult.MATCHED,
                recorded_location_id=asset.location_id,
                scanned_by=tara.id,
                scanned_at=min(next(times), end),
            )
        )
    for asset in wrong:
        session.add(
            InventoryScan(
                inventory_id=inventory.id,
                serial_number=asset.serial_number,
                asset_id=asset.id,
                result=InventoryScanResult.WRONG_LOCATION,
                scanned_location_id=zones[LocationType.STAGING].id
                if LocationType.STAGING in zones
                else None,
                recorded_location_id=asset.location_id,
                scanned_by=tara.id,
                scanned_at=min(next(times), end),
            )
        )
    for prefix in ("LM", "AP"):
        session.add(
            InventoryScan(
                inventory_id=inventory.id,
                serial_number=_fake_serial(rng, prefix),
                asset_id=None,
                result=InventoryScanResult.UNKNOWN,
                scanned_by=tara.id,
                scanned_at=min(next(times), end),
            )
        )
    await session.flush()


# --- call-off orders ------------------------------------------------------------------------------------

#: categories ordered regularly + typical quantities per call-off
ORDER_CATEGORIES = ("laptop", "docking_station", "monitor", "headset")


async def seed_calloff_orders(session: AsyncSession, ctx: SeedContext) -> None:
    rng = ctx.rng
    dana = await _user(session, ctx, DANA)
    hubs = (
        await session.scalars(
            select(Site).where(Site.kind == SiteKind.HUB).order_by(Site.sort_order)
        )
    ).all()
    recipient = get_settings().mail_partner_orders
    latest: dict[tuple[int, int], tuple[date, int]] = {}
    for days in (28, 14, 7):
        for hub in hubs:
            day = ctx.today - timedelta(days=days)
            if day.weekday() >= 5:
                day -= timedelta(days=day.weekday() - 4)
            sent_at = ctx.at(day, 10, 15)
            settings = (
                await session.scalars(
                    select(ArticleSiteSetting)
                    .join(Article, Article.id == ArticleSiteSetting.article_id)
                    .where(
                        ArticleSiteSetting.site_id == hub.id,
                        Article.is_active,
                        Article.category.in_(ORDER_CATEGORIES),
                    )
                    .order_by(Article.number)
                )
            ).all()
            chosen = sorted(
                rng.sample(list(settings), k=min(len(settings), rng.randint(3, 6))),
                key=lambda s: s.article.number,
            )
            order = CallOffOrder(
                number=f"CO-{hub.code}-{day:%Y%m%d}-01",
                site_id=hub.id,
                site=hub,
                status=CallOffOrderStatus.SENT,
                delivery_date=add_working_days(day, 3),
                note=None,
                recipient_email=recipient,
                created_by=dana.id,
                creator=dana,
                sent_at=sent_at,
                sent_by=dana.id,
                sender=dana,
                created_at=sent_at - timedelta(minutes=25),
            )
            for setting in chosen:
                demand = rng.randint(2, 14)
                stock = rng.randint(0, 10)
                buffer = setting.buffer_qty
                suggested = order_qty(demand, stock, buffer)
                quantity = max(1, suggested + rng.choice((0, 0, 0, 2, -1)))
                order.lines.append(
                    CallOffOrderLine(
                        article_id=setting.article_id,
                        article=setting.article,
                        demand_qty=demand,
                        stock_qty=stock,
                        buffer_qty=buffer,
                        suggested_qty=suggested,
                        quantity=quantity,
                    )
                )
                latest[(hub.id, setting.article_id)] = (day, quantity)
            session.add(order)
            await session.flush()
            doc = await document_for(session, order)
            xlsx = await store_file(
                session,
                order_xlsx(doc),
                f"{order.number}.xlsx",
                XLSX_TYPE,
                "calloff_document",
                entity=("calloff_order", order.id),
                created_by=dana,
            )
            pdf = await store_file(
                session,
                order_pdf(doc),
                f"{order.number}.pdf",
                PDF_TYPE,
                "calloff_document",
                entity=("calloff_order", order.id),
                created_by=dana,
            )
            order.xlsx_file_id, order.pdf_file_id = xlsx.id, pdf.id
    for (site_id, article_id), (day, quantity) in latest.items():
        setting = (
            await session.scalars(
                select(ArticleSiteSetting).where(
                    ArticleSiteSetting.site_id == site_id,
                    ArticleSiteSetting.article_id == article_id,
                )
            )
        ).one()
        setting.last_order_date = day
        setting.last_ordered_qty = quantity
    # today's Eastgate draft from the current matrix (the core seed already recalculated)
    eastgate = next(h for h in hubs if h.code == "EAS")
    draft = CallOffOrder(
        number=f"CO-EAS-{ctx.today:%Y%m%d}-01",
        site_id=eastgate.id,
        site=eastgate,
        status=CallOffOrderStatus.DRAFT,
        delivery_date=add_working_days(ctx.today, 3),
        note="Monitor 34 Ultrawide and Thunderbolt docks are short — please prioritise.",
        recipient_email=recipient,
        created_by=dana.id,
        creator=dana,
        created_at=ctx.now - timedelta(minutes=40),
    )
    settings = (
        await session.scalars(
            select(ArticleSiteSetting)
            .join(Article, Article.id == ArticleSiteSetting.article_id)
            .where(ArticleSiteSetting.site_id == eastgate.id, Article.is_active)
            .order_by(Article.number)
        )
    ).all()
    for setting in settings:
        line = line_from_setting(setting)
        if line.suggested_qty > 0:
            draft.lines.append(line)
    session.add(draft)
    await session.flush()


# --- deactivation batches -----------------------------------------------------------------------------------


async def seed_deactivations(session: AsyncSession, ctx: SeedContext) -> None:
    rng = ctx.rng
    dana = await _user(session, ctx, DANA)
    for batch_no, reason, extra_not_found in (
        (1, DeactivationReason.RETURNED_TO_LESSOR, 2),
        (2, DeactivationReason.BOUGHT_OUT, 0),
    ):
        moves = (
            await session.execute(
                select(
                    AssetMovement.asset_id,
                    AssetMovement.occurred_at,
                    AssetMovement.from_location_id,
                )
                .where(
                    AssetMovement.source_ref == f"deactivation:{batch_no}",
                    AssetMovement.action == AssetAction.DEACTIVATE,
                )
                .order_by(AssetMovement.id)
            )
        ).all()
        if not moves:
            continue
        at = min(m.occurred_at for m in moves) - timedelta(minutes=1)
        assets = {
            a.id: a
            for a in (
                await session.scalars(
                    select(Asset).where(Asset.id.in_([m.asset_id for m in moves]))
                )
            ).all()
        }
        locations = {loc.id: loc for loc in (await session.scalars(select(StockLocation))).all()}
        fake = [_fake_serial(rng, "QA") for _ in range(extra_not_found)]
        serials = [assets[m.asset_id].serial_number for m in moves] + fake
        rows = [
            [s, assets[m.asset_id].asset_tag or ""] for s, m in zip(serials, moves, strict=False)
        ]
        rows += [[s, ""] for s in fake]
        filename = (
            f"lessor-return-{at:%Y-%m-%d}.csv" if batch_no == 1 else f"buyout-{at:%Y-%m-%d}.csv"
        )
        upload = await store_file(
            session,
            csv_bytes(("serial_number", "asset_tag"), rows),
            filename,
            "text/csv",
            "deactivation_upload",
            created_by=dana,
        )
        batch = DeactivationBatch(
            reason=reason,
            original_filename=filename,
            file_id=upload.id,
            total_rows=len(rows),
            created_by=dana.id,
            creator=dana,
            created_at=at,
        )
        session.add(batch)
        await session.flush()
        if batch.id != batch_no:
            log.warning("h5_seed_batch_id_mismatch", expected=batch_no, got=batch.id)
        tally = Tally()
        for move in moves:
            asset = assets[move.asset_id]
            before = locations.get(move.from_location_id)
            tally.deactivated += 1
            tally.outcomes.append(
                Outcome(
                    asset.serial_number,
                    "deactivated",
                    f"{reason.value.replace('_', ' ').capitalize()} · was Return pending at "
                    f"{before.name if before else 'a hub'}",
                    asset,
                )
            )
        for serial in fake:
            tally.not_found += 1
            tally.outcomes.append(Outcome(serial, "not_found", "Not in the asset records."))
        tags = [a.asset_tag for a in assets.values() if a.asset_tag]
        tally.closed_leases = int(
            await session.scalar(
                select(func.count()).where(
                    LeaseReturn.asset_tag.in_(tags),
                    LeaseReturn.status == LeaseReturnStatus.CLOSED,
                    LeaseReturn.close_reason == "device_deactivated",
                )
            )
            or 0
        )
        await finish_batch(session, batch, tally, dana)
        assert len(batch.sample or []) <= SAMPLE_SIZE


__all__: list[Any] = ["seed_stock"]
