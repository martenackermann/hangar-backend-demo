"""Personalization: queue, FIFO candidates, assignment by scan, staging options (SPEC §6.6)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assets import asset_service
from app.core.enums import (
    AssetAction,
    AssetStatus,
    ItemSlot,
    LocationType,
    ProcurementType,
    ServiceRequestStatus,
    Trigger,
)
from app.core.errors import TransitionNotAllowed, ValidationProblem
from app.core.models import Asset, ServiceRequest, StockLocation, User
from app.core.read_models import (
    ServiceRequestItemOut,
    article_ref,
    asset_summary,
    build_item,
    build_sr_list_item,
    site_ref,
)
from app.core.rules import software_checklist
from app.core.schemas import ScanResult, UndoLink
from app.core.timeutil import business_today, now_utc, to_business_date
from app.core.transitions import check_version

from ..schemas import (
    AssetCandidate,
    AssetCandidates,
    PersonalizationQueueItem,
    StagingPatch,
    StockHint,
)
from .common import (
    business_date,
    conflict,
    find_asset,
    item_for,
    require_editor,
    require_new_slot,
    scan_result,
    slot_label,
    sr_search_clause,
)

SR = ServiceRequestStatus
QUEUE_STATUSES = (SR.APPOINTMENT_BOOKED, SR.PERSONALIZATION_IN_PROGRESS)
STAGING_EDITABLE = (SR.APPOINTMENT_BOOKED, SR.PERSONALIZATION_IN_PROGRESS, SR.PERSONALIZATION_DONE)
MAX_CANDIDATES = 50


# --- FIFO candidates (SPEC §6.6) ----------------------------------------------------------------


def candidates_stmt(article_id: int, site_id: int) -> Select[Asset]:
    return (
        select(Asset)
        .join(StockLocation, StockLocation.id == Asset.location_id)
        .where(
            Asset.status == AssetStatus.AVAILABLE,
            Asset.service_request_id.is_(None),
            Asset.installation_place.is_not(None),
            StockLocation.type == LocationType.STAGING,
            StockLocation.site_id == site_id,
            Asset.article_id == article_id,
        )
        .order_by(Asset.checked_in_at.asc().nulls_last(), Asset.created_at.asc(), Asset.id.asc())
    )


async def fifo_first(session: AsyncSession, article_id: int, site_id: int) -> Asset | None:
    return (await session.scalars(candidates_stmt(article_id, site_id).limit(1))).first()


async def asset_candidates(
    session: AsyncSession, sr: ServiceRequest, slot: ItemSlot
) -> AssetCandidates:
    require_new_slot(slot)
    item = item_for(sr, slot)
    site = sr.supplying_site
    if site is None:
        raise conflict(f"{sr.number} has no supplying hub (region unresolved).")
    site_out = site_ref(site)
    assert site_out is not None
    rows: Sequence[Asset] = []
    if item.article_id is not None:
        rows = (
            await session.scalars(candidates_stmt(item.article_id, site.id).limit(MAX_CANDIDATES))
        ).all()
    today = business_today()
    data: list[AssetCandidate] = []
    for rank, asset in enumerate(rows, start=1):
        summary = asset_summary(asset)
        assert summary is not None
        age = (today - to_business_date(asset.checked_in_at)).days if asset.checked_in_at else None
        data.append(
            AssetCandidate(
                asset=summary, checked_in_at=asset.checked_in_at, age_days=age, fifo_rank=rank
            )
        )
    return AssetCandidates(
        slot=slot,
        article=article_ref(item.article),
        site=site_out,
        suggested_asset_id=rows[0].id if rows else None,
        data=data,
    )


# --- queue (API.md §6.2) ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueFilters:
    site_id: int
    statuses: Sequence[SR] | None = None
    editor_id: int | str | None = None
    region_ids: Sequence[int | str] | None = None
    procurement_types: Sequence[ProcurementType] | None = None
    appointment_from: date | None = None
    appointment_to: date | None = None
    q: str | None = None


def queue_stmt(filters: QueueFilters, user: User) -> Select[ServiceRequest]:
    statuses = list(filters.statuses or QUEUE_STATUSES)
    bad = [s for s in statuses if s not in QUEUE_STATUSES]
    if bad:
        raise ValidationProblem(
            {"status": [f"Unknown queue status '{s}'." for s in bad]},
        )
    clauses: list[ColumnElement[bool]] = [
        ServiceRequest.supplying_site_id == filters.site_id,
        ServiceRequest.status.in_([str(s) for s in statuses]),
    ]
    if filters.editor_id == "me":
        clauses.append(ServiceRequest.editor_user_id == user.id)
    elif filters.editor_id == "none":
        clauses.append(ServiceRequest.editor_user_id.is_(None))
    elif isinstance(filters.editor_id, int):
        clauses.append(ServiceRequest.editor_user_id == filters.editor_id)
    clauses += region_clauses(filters.region_ids)
    if filters.procurement_types:
        clauses.append(
            ServiceRequest.procurement_type.in_([str(p) for p in filters.procurement_types])
        )
    clauses += appointment_range(filters.appointment_from, filters.appointment_to)
    search = sr_search_clause(filters.q)
    if search is not None:
        clauses.append(search)
    return select(ServiceRequest).where(*clauses)


def region_clauses(region_ids: Sequence[int | str] | None) -> list[ColumnElement[bool]]:
    if not region_ids:
        return []
    ids = [r for r in region_ids if isinstance(r, int)]
    parts: list[ColumnElement[bool]] = []
    if ids:
        parts.append(ServiceRequest.region_id.in_(ids))
    if "none" in region_ids:
        parts.append(ServiceRequest.region_id.is_(None))
    return [or_(*parts)] if parts else []


def appointment_range(start: date | None, end: date | None) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = []
    day = business_date(ServiceRequest.appointment_start_at)
    if start is not None:
        clauses.append(day >= start)
    if end is not None:
        clauses.append(day <= end)
    return clauses


async def stock_hints_for(
    session: AsyncSession, srs: Sequence[ServiceRequest]
) -> dict[tuple[int, int], tuple[int, Asset | None]]:
    """``(article_id, site_id) → (available count, FIFO first)`` for every new item on the page."""
    pairs = {
        (item.article_id, sr.supplying_site_id)
        for sr in srs
        for item in sr.new_items
        if item.article_id is not None and sr.supplying_site_id is not None
    }
    if not pairs:
        return {}
    article_ids = {a for a, _ in pairs}
    site_ids = {s for _, s in pairs}
    base = (
        select(
            Asset.id.label("asset_id"),
            Asset.article_id.label("article_id"),
            StockLocation.site_id.label("site_id"),
            func.row_number()
            .over(
                partition_by=(Asset.article_id, StockLocation.site_id),
                order_by=(
                    Asset.checked_in_at.asc().nulls_last(),
                    Asset.created_at.asc(),
                    Asset.id.asc(),
                ),
            )
            .label("rank"),
            func.count()
            .over(partition_by=(Asset.article_id, StockLocation.site_id))
            .label("available"),
        )
        .join(StockLocation, StockLocation.id == Asset.location_id)
        .where(
            Asset.status == AssetStatus.AVAILABLE,
            Asset.service_request_id.is_(None),
            Asset.installation_place.is_not(None),
            StockLocation.type == LocationType.STAGING,
            StockLocation.site_id.in_(site_ids),
            Asset.article_id.in_(article_ids),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(base.c.asset_id, base.c.article_id, base.c.site_id, base.c.available).where(
                base.c.rank == 1
            )
        )
    ).all()
    firsts = {row.asset_id for row in rows}
    assets = {
        a.id: a for a in (await session.scalars(select(Asset).where(Asset.id.in_(firsts)))).all()
    }
    return {
        (row.article_id, row.site_id): (int(row.available), assets.get(row.asset_id))
        for row in rows
    }


async def build_queue_items(
    session: AsyncSession, srs: Sequence[ServiceRequest], user: User
) -> list[PersonalizationQueueItem]:
    hints = await stock_hints_for(session, srs)
    result: list[PersonalizationQueueItem] = []
    for sr in srs:
        base = await build_sr_list_item(session, sr, user)
        stock: list[StockHint] = []
        for item in sr.new_items:
            count, first = (0, None)
            if item.article_id is not None and sr.supplying_site_id is not None:
                count, first = hints.get((item.article_id, sr.supplying_site_id), (0, None))
            stock.append(
                StockHint(
                    slot=ItemSlot(item.slot),
                    article=article_ref(item.article),
                    available_count=count,
                    suggested=asset_summary(first),
                )
            )
        result.append(
            PersonalizationQueueItem(
                **base.model_dump(by_alias=False),
                items=[build_item(i) for i in sr.items],
                stock_hints=stock,
                all_new_items_assigned=all(i.asset_id is not None for i in sr.new_items),
            )
        )
    return result


# --- assignment by scan (SPEC §6.6, SPEC_RESOLUTIONS #35) -----------------------------------------


def _check_assignable(sr: ServiceRequest) -> None:
    if sr.status != SR.PERSONALIZATION_IN_PROGRESS:
        raise TransitionNotAllowed(
            f"Devices can only be assigned while the request is being personalized "
            f"(status '{sr.status}').",
            action="assign",
            current_status=sr.status,
        )


def _device(asset: Asset) -> str:
    return asset.asset_tag or asset.serial_number


def _article_name(asset: Asset) -> str:
    return asset.article.name if asset.article else (asset.model or "Unknown model")


async def assign_by_scan(
    session: AsyncSession, sr: ServiceRequest, slot: ItemSlot, serial: str, actor: User
) -> ScanResult[ServiceRequestItemOut]:
    require_new_slot(slot)
    item = item_for(sr, slot)
    _check_assignable(sr)
    await require_editor(session, sr, actor)
    scanned = serial.strip().upper()
    href = f"/service-requests/{sr.id}/items/{slot}/assignment"

    def progress() -> tuple[int, int]:
        items = sr.new_items
        return sum(1 for i in items if i.asset_id is not None), len(items)

    def result(code: str, message: str, **kwargs: Any) -> ScanResult[ServiceRequestItemOut]:
        return scan_result(
            code, message, scanned, entity=build_item(item), progress=progress(), **kwargs
        )

    asset = await find_asset(session, serial)
    if asset is None:
        return result("unknown_serial", f"Serial {scanned} is not in stock records.")
    scanned = asset.serial_number
    if item.article_id is None:
        return result(
            "unknown_article",
            f"Ordered type '{item.requested_type}' is not in the article catalogue.",
        )
    if asset.article_id != item.article_id:
        expected = item.article.name if item.article else item.requested_type
        return result("wrong_article", f"Scanned {_article_name(asset)}, expected {expected}.")
    if item.asset_id == asset.id:
        return result("unchanged", f"{_device(asset)} is already assigned to {slot_label(slot)}.")
    if asset.status != AssetStatus.AVAILABLE or asset.service_request_id is not None:
        if asset.service_request is not None:
            other = asset.service_request
            where = "this request" if other.id == sr.id else other.number
            message = f"Asset is {asset.status} for {where}."
        else:
            message = f"Asset is {asset.status}."
        return result("not_available", message)
    location = asset.location
    site_id = sr.supplying_site_id
    if location.type != LocationType.STAGING or site_id is None or location.site_id != site_id:
        hub = sr.supplying_site.name if sr.supplying_site else "no hub"
        return result(
            "wrong_location", f"Asset is in {location.name}; this request is supplied by {hub}."
        )
    if asset.installation_place is None:
        return result("no_installation_place", "Scan an installation place for this asset first.")

    first = await fifo_first(session, item.article_id, site_id)
    previous = item.asset
    if previous is not None:
        await asset_service.move(
            session, previous, AssetAction.UNASSIGN, actor=actor, trigger=Trigger.USER
        )
    await asset_service.move(
        session, asset, AssetAction.ASSIGN, actor=actor, trigger=Trigger.USER, service_request=sr
    )
    item.asset = asset
    item.asset_id = asset.id
    item.assigned_at = now_utc()
    item.assigned_by = actor.id
    item.packed_at = None
    item.packed_by = None
    await session.flush()
    await session.refresh(item, ["asset", "assigned_by_user", "packed_by_user"])
    undo = UndoLink(method="DELETE", href=href, body=None)
    done = f"{_article_name(asset)} {_device(asset)} assigned to {slot_label(slot)}."
    if first is not None and first.id != asset.id:
        return result(
            "not_fifo",
            f"Assigned. Note: {_device(first)} ({first.installation_place}) was in stock longer.",
            applied=True,
            undo=undo,
        )
    return result("assigned", done, applied=True, undo=undo)


async def unassign(
    session: AsyncSession, sr: ServiceRequest, slot: ItemSlot, actor: User
) -> ServiceRequestItemOut:
    require_new_slot(slot)
    item = item_for(sr, slot)
    _check_assignable(sr)
    await require_editor(session, sr, actor)
    asset = item.asset
    if asset is not None:
        await asset_service.move(
            session, asset, AssetAction.UNASSIGN, actor=actor, trigger=Trigger.USER
        )
        item.asset = None
        item.asset_id = None
        item.assigned_at = None
        item.assigned_by = None
        item.packed_at = None
        item.packed_by = None
        await session.flush()
        await session.refresh(item, ["asset", "assigned_by_user", "packed_by_user"])
    return build_item(item)


# --- staging options ---------------------------------------------------------------------------


async def patch_staging(
    session: AsyncSession, sr: ServiceRequest, body: StagingPatch, actor: User
) -> None:
    check_version(sr, body.version)
    if sr.status not in STAGING_EDITABLE:
        raise conflict("Staging options are locked once the order is closed.")
    if sr.status == SR.PERSONALIZATION_IN_PROGRESS:
        await require_editor(session, sr, actor)
    if "os_reinstall" in body.model_fields_set:
        sr.os_reinstall = body.os_reinstall
    if body.added_software is not None:
        sr.added_software = software_checklist([], body.added_software, []).added
    await session.flush()


async def set_software_checklist(
    session: AsyncSession,
    sr: ServiceRequest,
    *,
    installed: list[str],
    added: list[str],
) -> None:
    """SPEC §6.15: editable in ``personalization_in_progress`` and ``personalization_done``."""
    if sr.status not in (SR.PERSONALIZATION_IN_PROGRESS, SR.PERSONALIZATION_DONE):
        raise conflict(
            "The software checklist can only change during personalization or order closing."
        )
    checklist = software_checklist(sr.requested_software, added, installed)
    sr.added_software = checklist.added
    sr.installed_software = checklist.installed
    await session.flush()
