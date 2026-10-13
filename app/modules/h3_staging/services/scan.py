"""Installation place scan station (SPEC §6.16, API.md §6.1)."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assets import asset_service, stock_location
from app.core.enums import AssetAction, AssetStatus, LocationType, Trigger
from app.core.errors import ApiError, NotFoundError
from app.core.loading import ensure_loaded
from app.core.models import Asset, AssetMovement, Site, StockLocation, User
from app.core.read_models import asset_summary
from app.core.rules import normalize_place, normalize_serial
from app.core.schemas import AssetSummary, ScanResult, UndoLink

from ..schemas import InstallationPlaceOccupancy
from .common import find_asset, get_hub, scan_result

SOURCE_REF = "scan:installation_place"
UNDO_HREF = "/scan/installation-place/undo"
_IN_STOCK = (LocationType.GOODS_IN, LocationType.STAGING)


def _summary(asset: Asset) -> AssetSummary:
    summary = asset_summary(asset)
    assert summary is not None
    return summary


def _device(asset: Asset) -> str:
    return asset.asset_tag or asset.serial_number


async def scan_installation_place(
    session: AsyncSession, *, site_id: int, serial: str, place: str, actor: User
) -> ScanResult[AssetSummary]:
    """One request per (serial, place) pair; business misses are verdicts, not errors."""
    site = await get_hub(session, site_id)
    scanned = normalize_serial(serial) or serial.strip().upper()
    normalized_place = normalize_place(place)
    if normalized_place is None:
        shown = place.strip().upper()
        return scan_result(
            "invalid_place",
            f"'{shown}' is not an installation place (expected e.g. B4-12).",
            scanned,
        )

    asset = await find_asset(session, serial)
    if asset is None:
        return scan_result("unknown_serial", f"Serial {scanned} is not in stock records.", scanned)
    await ensure_loaded(session, asset)
    scanned = asset.serial_number

    if asset.status != AssetStatus.AVAILABLE or asset.service_request_id is not None:
        message = f"{_device(asset)} is {asset.status}"
        if asset.service_request is not None:
            message = f"Asset is {asset.status} for {asset.service_request.number}"
        return scan_result("not_available", f"{message}.", scanned, entity=_summary(asset))

    location = asset.location
    if location.type not in _IN_STOCK:
        return scan_result(
            "not_in_stock",
            f"{_device(asset)} is in {location.name}, not in hub stock.",
            scanned,
            entity=_summary(asset),
        )

    staging = await stock_location(session, site.id, LocationType.STAGING)
    if location.id == staging.id and asset.installation_place == normalized_place:
        return scan_result(
            "unchanged",
            f"{_device(asset)} is already on {normalized_place}.",
            scanned,
            entity=_summary(asset),
        )

    warnings: list[tuple[str, str]] = []
    if location.site_id != site.id:
        warnings.append(
            (
                "moved_from_other_site",
                f"Moved from {location.name} — the asset is physically at {site.name}.",
            )
        )
    occupant = (
        await session.scalars(
            select(Asset)
            .where(
                Asset.location_id == staging.id,
                Asset.installation_place == normalized_place,
                Asset.status == AssetStatus.AVAILABLE,
                Asset.id != asset.id,
            )
            .order_by(Asset.id)
            .limit(1)
        )
    ).first()
    if occupant is not None:
        warnings.append(("place_occupied", f"{normalized_place} also holds {_device(occupant)}."))
    if asset.article_id is None:
        warnings.append(
            (
                "unknown_model",
                f"Model '{asset.model or 'unknown'}' is not in the article catalogue.",
            )
        )

    movement = await asset_service.move(
        session,
        asset,
        AssetAction.PLACE,
        actor=actor,
        trigger=Trigger.USER,
        to_location=staging,
        installation_place=normalized_place,
        source_ref=SOURCE_REF,
    )
    placed = f"{_device(asset)} → {normalized_place} · {staging.name}"
    undo = UndoLink(method="POST", href=UNDO_HREF, body={"movementId": movement.id})
    if warnings:
        code, note = warnings[0]
        extra = " ".join(text for _, text in warnings[1:])
        message = f"{placed}. {note}" + (f" {extra}" if extra else "")
        return scan_result(code, message, scanned, applied=True, entity=_summary(asset), undo=undo)
    return scan_result("placed", placed, scanned, applied=True, entity=_summary(asset), undo=undo)


async def undo_installation_place(
    session: AsyncSession, *, movement_id: int, actor: User
) -> ScanResult[AssetSummary]:
    """Revert a ``place`` movement if it is still the asset's latest movement (SPEC §6.16)."""
    movement = await session.get(AssetMovement, movement_id)
    if movement is None:
        raise NotFoundError(f"Movement {movement_id} not found.")
    if movement.action != AssetAction.PLACE:
        raise _undo_impossible("Only installation place scans can be undone here.")
    asset = (
        await session.scalars(
            select(Asset)
            .where(Asset.id == movement.asset_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one()
    await ensure_loaded(session, asset)
    latest_id = (
        await session.scalars(
            select(AssetMovement.id)
            .where(AssetMovement.asset_id == asset.id)
            .order_by(AssetMovement.occurred_at.desc(), AssetMovement.id.desc())
            .limit(1)
        )
    ).one()
    if latest_id != movement.id:
        raise _undo_impossible("The asset was moved again since this scan.")
    if asset.status != AssetStatus.AVAILABLE or asset.service_request_id is not None:
        raise _undo_impossible("The asset is no longer available.")

    from_location = movement.from_location
    if from_location is None:  # pragma: no cover - place always has a source location
        raise _undo_impossible("The scan has no previous location.")
    target: dict[str, Any] = {
        "to_location": await session.get(StockLocation, from_location.id),
        "installation_place": movement.from_installation_place,
    }
    if from_location.type == LocationType.STAGING and movement.from_installation_place:
        await asset_service.move(
            session,
            asset,
            AssetAction.PLACE,
            actor=actor,
            trigger=Trigger.USER,
            source_ref=f"undo:{movement.id}",
            **target,
        )
    else:
        # Back to goods-in (or an unplaced staging slot): no movement action describes that,
        # so the revert is recorded as a reasoned correction.
        await asset_service.move(
            session,
            asset,
            AssetAction.CORRECT,
            actor=actor,
            trigger=Trigger.USER,
            reason=f"Undo of installation place scan (movement {movement.id})",
            source_ref=f"undo:{movement.id}",
            **target,
        )
    where = movement.from_installation_place or from_location.name
    return scan_result(
        "undone",
        f"Undone: {_device(asset)} is back at {where}.",
        asset.serial_number,
        applied=True,
        entity=_summary(asset),
    )


def _undo_impossible(detail: str) -> ApiError:
    return ApiError(detail, status=409, code="undo_not_possible", title="Undo not possible")


_DIGITS = re.compile(r"(\d+)")


def _place_key(place: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in _DIGITS.split(place)]


async def installation_places(
    session: AsyncSession, *, site_id: int | None, q: str | None
) -> list[InstallationPlaceOccupancy]:
    """Places with ≥ 1 asset in a hub's staging location; ``q`` = place prefix."""
    stmt = (
        select(Asset)
        .join(StockLocation, StockLocation.id == Asset.location_id)
        .where(StockLocation.type == LocationType.STAGING, Asset.installation_place.is_not(None))
    )
    if site_id is not None:
        site = await session.get(Site, site_id)
        if site is None:
            raise NotFoundError(f"Site {site_id} not found.")
        stmt = stmt.where(StockLocation.site_id == site_id)
    if q and q.strip():
        prefix = q.strip().upper()
        if prefix.startswith("LOC:"):
            prefix = prefix[4:].strip()
        stmt = stmt.where(Asset.installation_place.startswith(prefix, autoescape=True))
    assets = (await session.scalars(stmt.order_by(Asset.installation_place, Asset.id))).all()
    grouped: dict[str, list[AssetSummary]] = {}
    for asset in assets:
        assert asset.installation_place is not None
        grouped.setdefault(asset.installation_place, []).append(_summary(asset))
    return [
        InstallationPlaceOccupancy(place=place, assets=grouped[place])
        for place in sorted(grouped, key=_place_key)
    ]
