"""Inventories: scans, live reconciliation, close/cancel (SPEC §4.6, §6.9; API.md §8.5).

Binding resolutions: #25 (``duplicate`` is a *warning*), #27 (matched assets in status
``missing`` are corrected on close and shown as correctable), #28 (Campus inventories,
fallback location ``CMP-RETURN``). Core decision: an unknown serial is recorded with scan
code ``unknown_recorded`` (warning).

The expected set **E** of a site is evaluated live: assets located at the site (goods in,
staging, goods out, returns) whose status is not deactivated (``missing`` included).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import and_, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assets import UNSET, asset_service
from app.core.auth import can
from app.core.enums import (
    DEACTIVATED,
    AssetAction,
    AssetStatus,
    InventoryScanResult,
    InventoryStatus,
    LocationType,
    ScanVerdict,
    SiteKind,
    Trigger,
    label,
)
from app.core.errors import (
    ApiError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    TransitionNotAllowed,
    ValidationProblem,
)
from app.core.files import store_file
from app.core.models import Asset, Site, StockLocation, User
from app.core.notifications import notify
from app.core.notifications.renderer import format_time
from app.core.pagination import PageParams
from app.core.read_models.refs import asset_summary, location_ref, site_ref, user_ref
from app.core.rules import normalize_serial
from app.core.schemas import EntityRef, ScanProgress, ScanResult, UndoLink
from app.core.timeutil import business_today, now_utc

from ..models import Inventory, InventoryScan
from ..schemas import (
    InventoryCounts,
    InventoryDetail,
    InventoryProgress,
    InventoryScanOut,
    InventorySummary,
    Reconciliation,
    ReconciliationCategory,
    ReconciliationItem,
)
from .common import csv_bytes, schedule_recalculation, team_mailbox

R = InventoryScanResult
S = InventoryStatus
A = AssetStatus
EXPECTED_TYPES = (
    LocationType.GOODS_IN,
    LocationType.STAGING,
    LocationType.GOODS_OUT,
    LocationType.RETURN,
)
CATEGORY_ORDER: dict[str, int] = {"missing": 0, "wrong_location": 1, "unknown": 2, "matched": 3}
RELOCATABLE = frozenset({A.AVAILABLE, A.COLLECTED, A.MISSING})
MISSABLE = frozenset({A.AVAILABLE, A.COLLECTED})
TEMPLATE_ID = "inventory.report"
REPORT_HEADER = (
    "Serial",
    "Asset tag",
    "Article",
    "Status",
    "Recorded location",
    "Scanned location",
    "Scanned by",
    "Scanned at",
)
type ReportKind = Literal["missing", "wrong_location", "unknown"]
REPORT_KINDS: tuple[ReportKind, ...] = ("missing", "wrong_location", "unknown")


def entity_ref(inventory: Inventory) -> EntityRef:
    return EntityRef(type="inventory", id=inventory.id, label=f"Inventory {inventory.site.name}")


# --- expected set ------------------------------------------------------------------------------


def expected_clause(site_id: int) -> Any:
    locations = select(StockLocation.id).where(
        StockLocation.site_id == site_id, StockLocation.type.in_(EXPECTED_TYPES)
    )
    return and_(Asset.location_id.in_(locations), Asset.status.not_in(DEACTIVATED))


async def expected_assets(session: AsyncSession, site_id: int) -> list[Asset]:
    stmt = select(Asset).where(expected_clause(site_id)).order_by(Asset.serial_number)
    return list((await session.scalars(stmt)).all())


async def live_counts(session: AsyncSession, inventory: Inventory) -> InventoryCounts:
    in_e = expected_clause(inventory.site_id)
    expected = int(await session.scalar(select(func.count()).select_from(Asset).where(in_e)) or 0)
    base = (
        select(func.count())
        .select_from(InventoryScan)
        .where(InventoryScan.inventory_id == inventory.id)
    )
    scanned_in_e = int(
        await session.scalar(
            select(func.count(distinct(InventoryScan.asset_id)))
            .join(Asset, Asset.id == InventoryScan.asset_id)
            .where(InventoryScan.inventory_id == inventory.id, in_e)
        )
        or 0
    )
    matched = int(
        await session.scalar(
            base.join(Asset, Asset.id == InventoryScan.asset_id).where(
                InventoryScan.result == R.MATCHED, in_e
            )
        )
        or 0
    )
    wrong = int(await session.scalar(base.where(InventoryScan.result == R.WRONG_LOCATION)) or 0)
    unknown = int(await session.scalar(base.where(InventoryScan.result == R.UNKNOWN)) or 0)
    return InventoryCounts(
        expected=expected,
        matched=matched,
        missing=max(0, expected - scanned_in_e),
        wrong_location=wrong,
        unknown=unknown,
        corrected=None,
    )


def counts_of(inventory: Inventory) -> InventoryCounts | None:
    if inventory.status == S.OPEN or not inventory.result_summary:
        return None
    return InventoryCounts.model_validate(inventory.result_summary)


async def summary_counts(session: AsyncSession, inventory: Inventory) -> InventoryCounts:
    return counts_of(inventory) or await live_counts(session, inventory)


# --- site / zones -------------------------------------------------------------------------------


async def site_locations(session: AsyncSession, site_id: int) -> list[StockLocation]:
    stmt = (
        select(StockLocation)
        .where(StockLocation.site_id == site_id, StockLocation.type.in_(EXPECTED_TYPES))
        .order_by(StockLocation.sort_order, StockLocation.id)
    )
    return list((await session.scalars(stmt)).all())


async def get_inventory(
    session: AsyncSession, inventory_id: int, *, lock: bool = False
) -> Inventory:
    stmt = select(Inventory).where(Inventory.id == inventory_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    inventory = (await session.scalars(stmt)).first()
    if inventory is None:
        raise NotFoundError(f"Inventory {inventory_id} not found.")
    return inventory


async def open_inventory(
    session: AsyncSession, site_id: int, note: str | None, actor: User
) -> Inventory:
    site = await session.get(Site, site_id)
    if site is None or not site.is_active or site.kind not in (SiteKind.HUB, SiteKind.CAMPUS):
        raise ValidationProblem({"siteId": ["Choose a hub or the Campus."]})
    existing = (
        await session.scalars(
            select(Inventory.id).where(Inventory.site_id == site.id, Inventory.status == S.OPEN)
        )
    ).first()
    if existing is not None:
        raise ConflictError(
            f"An inventory is already open for {site.name} (#{existing}).",
            code="inventory_already_open",
            inventoryId=existing,
        )
    inventory = Inventory(
        site_id=site.id,
        site=site,
        status=S.OPEN,
        opened_at=now_utc(),
        opened_by=actor.id,
        opener=actor,
        note=(note or "").strip() or None,
    )
    session.add(inventory)
    await session.flush()
    return inventory


# --- scans --------------------------------------------------------------------------------------


def _require_open(inventory: Inventory) -> None:
    if inventory.status != S.OPEN:
        raise ConflictError(
            f"The inventory is {label(inventory.status).lower()}; scans are no longer possible."
        )


def build_scan(scan: InventoryScan) -> InventoryScanOut:
    scanner = user_ref(scan.scanner)
    assert scanner is not None
    return InventoryScanOut(
        id=scan.id,
        serial_number=scan.serial_number,
        result=scan.result,
        asset=asset_summary(scan.asset),
        scanned_location=location_ref(scan.scanned_location),
        recorded_location=location_ref(scan.recorded_location),
        scanned_by=scanner,
        scanned_at=scan.scanned_at,
    )


async def _progress(session: AsyncSession, inventory: Inventory) -> ScanProgress:
    counts = await live_counts(session, inventory)
    return ScanProgress(done=counts.expected - counts.missing, total=counts.expected)


def _describe(asset: Asset) -> str:
    article = asset.article.name if asset.article else asset.model or "Unknown model"
    return article


def _matched_message(asset: Asset) -> str:
    place = f" {asset.installation_place}" if asset.installation_place else ""
    return f"{_describe(asset)} · {label(asset.location.type)}{place}"


def _elsewhere_message(asset: Asset) -> str:
    location = asset.location
    if location.site_id is None:
        return f"Recorded as {label(asset.status)} ({location.name})."
    return f"Recorded at {location.name}."


async def _find_asset(session: AsyncSession, value: str) -> Asset | None:
    asset = (await session.scalars(select(Asset).where(Asset.serial_number == value))).first()
    if asset is None:
        asset = (await session.scalars(select(Asset).where(Asset.asset_tag == value))).first()
    return asset


async def scan(
    session: AsyncSession,
    inventory: Inventory,
    raw_serial: str,
    zone_id: int | None,
    actor: User,
) -> ScanResult[InventoryScanOut]:
    value = normalize_serial(raw_serial)
    if not value:
        raise ValidationProblem({"serial": ["Scan or type a serial number."]})
    _require_open(inventory)
    zone: StockLocation | None = None
    if zone_id is not None:
        zone = await session.get(StockLocation, zone_id)
        if zone is None or zone.site_id != inventory.site_id or zone.type not in EXPECTED_TYPES:
            raise ValidationProblem({"stockLocationId": ["Choose a zone of this site."]})
    asset = await _find_asset(session, value)
    serial = asset.serial_number if asset is not None else value
    clauses = [InventoryScan.serial_number == serial]
    if asset is not None:
        clauses.append(InventoryScan.asset_id == asset.id)
    previous = (
        await session.scalars(
            select(InventoryScan)
            .where(InventoryScan.inventory_id == inventory.id)
            .where(clauses[0] if len(clauses) == 1 else (clauses[0] | clauses[1]))
        )
    ).first()
    if previous is not None:
        who = previous.scanner.display_name if previous.scanner else "someone"
        return ScanResult[InventoryScanOut](
            verdict=ScanVerdict.WARNING,
            code="duplicate",
            message=f"Already scanned {format_time(previous.scanned_at)} by {who}.",
            scanned_value=value,
            applied=False,
            entity=build_scan(previous),
            progress=await _progress(session, inventory),
        )

    if asset is None:
        result, verdict, code = R.UNKNOWN, ScanVerdict.WARNING, "unknown_recorded"
        message = "Unknown serial — recorded as unexpected."
    elif asset.location.site_id == inventory.site_id and asset.status not in DEACTIVATED:
        if zone is None or zone.id == asset.location_id:
            result, verdict, code = R.MATCHED, ScanVerdict.OK, "matched"
            message = _matched_message(asset)
            if asset.status == A.MISSING:
                message += " — was marked missing; found again."
        else:
            result, verdict, code = R.WRONG_LOCATION, ScanVerdict.WARNING, "wrong_zone"
            message = f"Recorded in {label(asset.location.type)}, scanned in {label(zone.type)}."
    else:
        result, verdict, code = R.WRONG_LOCATION, ScanVerdict.WARNING, "wrong_site"
        message = _elsewhere_message(asset)

    record = InventoryScan(
        inventory_id=inventory.id,
        serial_number=serial,
        asset_id=asset.id if asset is not None else None,
        asset=asset,
        result=result,
        scanned_location_id=zone.id if zone else None,
        scanned_location=zone,
        recorded_location_id=asset.location_id if asset is not None else None,
        recorded_location=asset.location if asset is not None else None,
        scanned_by=actor.id,
        scanner=actor,
        scanned_at=now_utc(),
    )
    session.add(record)
    await session.flush()
    return ScanResult[InventoryScanOut](
        verdict=verdict,
        code=code,
        message=message,
        scanned_value=value,
        applied=True,
        entity=build_scan(record),
        undo=UndoLink(
            method="DELETE", href=f"/inventories/{inventory.id}/scans/{record.id}", body=None
        ),
        progress=await _progress(session, inventory),
    )


async def undo_scan(session: AsyncSession, inventory: Inventory, scan_id: int, actor: User) -> None:
    record = await session.get(InventoryScan, scan_id)
    if record is None or record.inventory_id != inventory.id:
        raise NotFoundError(f"Scan {scan_id} not found in this inventory.")
    if inventory.status != S.OPEN:
        raise ApiError(
            "The inventory is no longer open; scans cannot be undone.",
            status=409,
            code="undo_not_possible",
        )
    if record.scanned_by != actor.id and not can(actor, "inventory.manage"):
        raise ForbiddenError("You can only undo your own scans.")
    await session.delete(record)
    await session.flush()


async def recent_scans(
    session: AsyncSession, inventory: Inventory, limit: int = 20
) -> list[InventoryScan]:
    stmt = (
        select(InventoryScan)
        .where(InventoryScan.inventory_id == inventory.id)
        .order_by(InventoryScan.scanned_at.desc(), InventoryScan.id.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


# --- reconciliation ----------------------------------------------------------------------------


@dataclass(slots=True)
class Correction:
    action: AssetAction
    target: StockLocation | None = None
    place: Any = UNSET


@dataclass(slots=True)
class Entry:
    category: ReconciliationCategory
    serial: str
    asset: Asset | None
    status: AssetStatus | None
    recorded_location: StockLocation | None
    scanned_location: StockLocation | None
    scanned_by: User | None
    scanned_at: datetime | None
    correction: Correction | None = None
    corrected: bool = False


def _default_target(status: AssetStatus, locations: list[StockLocation]) -> StockLocation | None:
    by_type = {loc.type: loc for loc in locations}
    if status in (A.AVAILABLE, A.MISSING) and LocationType.GOODS_IN in by_type:
        return by_type[LocationType.GOODS_IN]
    return by_type.get(LocationType.RETURN)


def _relocation(
    asset: Asset, zone: StockLocation | None, locations: list[StockLocation]
) -> Correction | None:
    if asset.status not in RELOCATABLE:
        return None
    target = zone or _default_target(asset.status, locations)
    if target is None or (target.id == asset.location_id and asset.status != A.MISSING):
        return None
    place: Any = None
    if target.id == asset.location_id and target.type == LocationType.STAGING:
        place = asset.installation_place
    return Correction(AssetAction.INVENTORY_RELOCATE, target, place)


async def live_entries(session: AsyncSession, inventory: Inventory) -> list[Entry]:
    scans = (
        await session.scalars(
            select(InventoryScan).where(InventoryScan.inventory_id == inventory.id)
        )
    ).all()
    expected = await expected_assets(session, inventory.site_id)
    expected_ids = {a.id for a in expected}
    scanned_ids = {s.asset_id for s in scans if s.asset_id is not None}
    locations = await site_locations(session, inventory.site_id)
    entries: list[Entry] = []
    for record in scans:
        asset = record.asset
        if record.result == R.MATCHED:
            if asset is None or asset.id not in expected_ids:
                continue
            correction = (
                Correction(
                    AssetAction.INVENTORY_RELOCATE,
                    asset.location,
                    asset.installation_place
                    if asset.location.type == LocationType.STAGING
                    else None,
                )
                if asset.status == A.MISSING
                else None
            )
            entries.append(_entry("matched", record, asset, correction))
        elif record.result == R.WRONG_LOCATION:
            correction = _relocation(asset, record.scanned_location, locations) if asset else None
            entries.append(_entry("wrong_location", record, asset, correction))
        else:
            entries.append(_entry("unknown", record, asset, None))
    for asset in expected:
        if asset.id in scanned_ids:
            continue
        correction = Correction(AssetAction.MARK_MISSING) if asset.status in MISSABLE else None
        entries.append(
            Entry(
                category="missing",
                serial=asset.serial_number,
                asset=asset,
                status=asset.status,
                recorded_location=asset.location,
                scanned_location=None,
                scanned_by=None,
                scanned_at=None,
                correction=correction,
            )
        )
    entries.sort(key=lambda e: (CATEGORY_ORDER[e.category], e.serial))
    return entries


def _entry(
    category: ReconciliationCategory,
    record: InventoryScan,
    asset: Asset | None,
    correction: Correction | None,
) -> Entry:
    return Entry(
        category=category,
        serial=record.serial_number,
        asset=asset,
        status=asset.status if asset else None,
        recorded_location=asset.location if asset else record.recorded_location,
        scanned_location=record.scanned_location,
        scanned_by=record.scanner,
        scanned_at=record.scanned_at,
        correction=correction,
    )


def _frozen(entry: Entry) -> dict[str, Any]:
    return {
        "category": entry.category,
        "serialNumber": entry.serial,
        "assetId": entry.asset.id if entry.asset else None,
        "status": str(entry.status) if entry.status else None,
        "recordedLocationId": entry.recorded_location.id if entry.recorded_location else None,
        "scannedLocationId": entry.scanned_location.id if entry.scanned_location else None,
        "scannedById": entry.scanned_by.id if entry.scanned_by else None,
        "scannedAt": entry.scanned_at.isoformat() if entry.scanned_at else None,
        "correctable": entry.correction is not None,
        "corrected": entry.corrected,
    }


async def frozen_entries(session: AsyncSession, inventory: Inventory) -> list[Entry]:
    raw: list[dict[str, Any]] = list(inventory.reconciliation or [])
    asset_ids = {r["assetId"] for r in raw if r.get("assetId")}
    location_ids = {
        r[k] for r in raw for k in ("recordedLocationId", "scannedLocationId") if r.get(k)
    }
    user_ids = {r["scannedById"] for r in raw if r.get("scannedById")}
    assets = (
        {
            a.id: a
            for a in (await session.scalars(select(Asset).where(Asset.id.in_(asset_ids)))).all()
        }
        if asset_ids
        else {}
    )
    locations = (
        {
            loc.id: loc
            for loc in (
                await session.scalars(
                    select(StockLocation).where(StockLocation.id.in_(location_ids))
                )
            ).all()
        }
        if location_ids
        else {}
    )
    users = (
        {u.id: u for u in (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()}
        if user_ids
        else {}
    )
    entries: list[Entry] = []
    for r in raw:
        entry = Entry(
            category=r["category"],
            serial=r["serialNumber"],
            asset=assets.get(r.get("assetId") or 0),
            status=AssetStatus(r["status"]) if r.get("status") else None,
            recorded_location=locations.get(r.get("recordedLocationId") or 0),
            scanned_location=locations.get(r.get("scannedLocationId") or 0),
            scanned_by=users.get(r.get("scannedById") or 0),
            scanned_at=datetime.fromisoformat(r["scannedAt"]) if r.get("scannedAt") else None,
            correction=Correction(AssetAction.CORRECT) if r.get("correctable") else None,
            corrected=bool(r.get("corrected")),
        )
        entries.append(entry)
    return entries


async def entries_for(session: AsyncSession, inventory: Inventory) -> list[Entry]:
    if inventory.status == S.OPEN or inventory.reconciliation is None:
        return await live_entries(session, inventory)
    return await frozen_entries(session, inventory)


def build_item(entry: Entry) -> ReconciliationItem:
    return ReconciliationItem(
        serial_number=entry.serial,
        asset=asset_summary(entry.asset),
        category=entry.category,
        recorded_location=location_ref(entry.recorded_location),
        scanned_location=location_ref(entry.scanned_location),
        scanned_by=user_ref(entry.scanned_by),
        scanned_at=entry.scanned_at,
        correctable=entry.correction is not None,
    )


async def reconciliation(
    session: AsyncSession,
    inventory: Inventory,
    category: ReconciliationCategory | None,
    paging: PageParams,
) -> Reconciliation:
    entries = await entries_for(session, inventory)
    summary = await summary_counts(session, inventory)
    if category is not None:
        entries = [e for e in entries if e.category == category]
    page = entries[paging.offset : paging.offset + paging.per_page]
    return Reconciliation(
        data=[build_item(e) for e in page], meta=paging.meta(len(entries)), summary=summary
    )


# --- close / cancel -------------------------------------------------------------------------------


def _require_open_action(inventory: Inventory, action: str) -> None:
    if inventory.status != S.OPEN:
        raise TransitionNotAllowed(
            f"Action '{action}' is not allowed while the inventory is '{inventory.status}'.",
            action=action,
            current_status=inventory.status,
            allowed_actions=[],
        )


def report_rows(entries: list[Entry], category: str) -> list[list[Any]]:
    return [
        [
            e.serial,
            e.asset.asset_tag if e.asset else None,
            (
                e.asset.article.name
                if e.asset and e.asset.article
                else (e.asset.model if e.asset else None)
            ),
            label(e.status) if e.status else None,
            e.recorded_location.name if e.recorded_location else None,
            e.scanned_location.name if e.scanned_location else None,
            e.scanned_by.display_name if e.scanned_by else None,
            e.scanned_at,
        ]
        for e in entries
        if e.category == category
    ]


async def apply_corrections(
    session: AsyncSession,
    inventory: Inventory,
    entries: list[Entry],
    actor: User | None,
    trigger: Trigger,
) -> int:
    corrected = 0
    ordered = sorted(entries, key=lambda e: 0 if e.category == "missing" else 1)
    for entry in ordered:
        correction = entry.correction
        if correction is None or entry.asset is None:
            continue
        await asset_service.move(
            session,
            entry.asset,
            correction.action,
            actor=actor,
            trigger=trigger,
            to_location=correction.target,
            installation_place=correction.place,
            reason=f"Inventory {inventory.site.name} #{inventory.id}",
            source_ref=f"inventory:{inventory.id}",
        )
        entry.corrected = True
        corrected += 1
    return corrected


async def store_reports(
    session: AsyncSession, inventory: Inventory, entries: list[Entry], actor: User | None
) -> None:
    files = {}
    for kind in REPORT_KINDS:
        stored = await store_file(
            session,
            csv_bytes(REPORT_HEADER, report_rows(entries, kind)),
            f"inventory-{inventory.id}-{inventory.site.code.lower()}-{kind.replace('_', '-')}.csv",
            "text/csv",
            "inventory_report",
            entity=entity_ref(inventory),
            created_by=actor,
        )
        files[kind] = stored.id
    inventory.missing_report_file_id = files["missing"]
    inventory.wrong_location_report_file_id = files["wrong_location"]
    inventory.unknown_report_file_id = files["unknown"]


async def close_inventory(
    session: AsyncSession,
    inventory: Inventory,
    count_date: date,
    apply: bool,
    actor: User,
    *,
    closed_at: datetime | None = None,
    send_mail: bool = True,
) -> Inventory:
    _require_open_action(inventory, "close")
    if count_date > business_today():
        raise ValidationProblem({"countDate": ["The count date cannot be in the future."]})
    entries = await live_entries(session, inventory)
    counts = await live_counts(session, inventory)  # frozen *before* corrections move assets
    if apply:
        counts.corrected = await apply_corrections(session, inventory, entries, actor, Trigger.USER)
    await store_reports(session, inventory, entries, actor)
    inventory.result_summary = counts.model_dump(by_alias=True)
    inventory.reconciliation = [_frozen(e) for e in entries]
    inventory.status = S.CLOSED
    inventory.closed_at = closed_at or now_utc()
    inventory.closed_by = actor.id
    inventory.closer = actor
    inventory.count_date = count_date
    inventory.apply_corrections = apply
    await session.flush()
    if send_mail:
        await notify(
            session,
            TEMPLATE_ID,
            to=[team_mailbox()],
            context=report_context(inventory, counts, actor),
            entity=entity_ref(inventory),
            attachments=[
                f
                for f in (
                    inventory.missing_report_file_id,
                    inventory.wrong_location_report_file_id,
                    inventory.unknown_report_file_id,
                )
                if f is not None
            ],
            actor=actor,
        )
        schedule_recalculation(session)
    return inventory


def report_context(inventory: Inventory, counts: InventoryCounts, actor: User) -> dict[str, Any]:
    return {
        "inventory_id": inventory.id,
        "site_name": inventory.site.name,
        "count_date": (inventory.count_date or business_today()).isoformat(),
        "expected": counts.expected,
        "matched": counts.matched,
        "missing": counts.missing,
        "wrong_location": counts.wrong_location,
        "unknown": counts.unknown,
        "corrected": counts.corrected or 0,
        "apply_corrections": bool(inventory.apply_corrections),
        "closed_by": actor.display_name,
        "opened_at": inventory.opened_at.isoformat(),
    }


async def cancel_inventory(
    session: AsyncSession, inventory: Inventory, comment: str | None, actor: User
) -> Inventory:
    _require_open_action(inventory, "cancel")
    entries = await live_entries(session, inventory)
    counts = await live_counts(session, inventory)
    inventory.result_summary = counts.model_dump(by_alias=True)
    inventory.reconciliation = [_frozen(e) for e in entries]
    inventory.status = S.CANCELLED
    inventory.closed_at = now_utc()
    inventory.closed_by = actor.id
    inventory.closer = actor
    if comment and comment.strip():
        cancelled = f"Cancelled by {actor.display_name}: {comment.strip()}"
        inventory.note = f"{inventory.note}\n{cancelled}" if inventory.note else cancelled
    await session.flush()
    return inventory


# --- read models -------------------------------------------------------------------------------------


async def build_summary(session: AsyncSession, inventory: Inventory) -> InventorySummary:
    site = site_ref(inventory.site)
    opener = user_ref(inventory.opener)
    assert site is not None
    assert opener is not None
    return InventorySummary(
        id=inventory.id,
        site=site,
        status=inventory.status,
        opened_at=inventory.opened_at,
        opened_by=opener,
        closed_at=inventory.closed_at,
        closed_by=user_ref(inventory.closer),
        count_date=inventory.count_date,
        summary=await summary_counts(session, inventory),
    )


async def build_detail(
    session: AsyncSession, inventory: Inventory, user: User | None
) -> InventoryDetail:
    summary = await build_summary(session, inventory)
    counts = summary.summary
    scanned = counts.expected - counts.missing
    percent = round(scanned * 100 / counts.expected) if counts.expected else 0
    actions = (
        ["close", "cancel"]
        if inventory.status == S.OPEN and user is not None and can(user, "inventory.manage")
        else []
    )
    zones = [
        ref
        for loc in await site_locations(session, inventory.site_id)
        if (ref := location_ref(loc))
    ]
    return InventoryDetail(
        **summary.model_dump(by_alias=False),
        note=inventory.note,
        apply_corrections=inventory.apply_corrections,
        version=inventory.version,
        progress=InventoryProgress(scanned=scanned, expected=counts.expected, percent=percent),
        zones=zones,
        recent_scans=[build_scan(s) for s in await recent_scans(session, inventory)],
        available_actions=actions,
    )
