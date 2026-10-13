"""Assets list/detail/correction/export (API.md §8.3) and the asset action handlers (#24)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.assets import UNSET, asset_service
from app.core.enums import (
    ArticleCategory,
    AssetAction,
    AssetSource,
    AssetStatus,
    LocationType,
    Trigger,
    label,
)
from app.core.errors import ConflictError, NotFoundError, ValidationProblem
from app.core.models import Article, Asset, StockLocation, User
from app.core.pagination import search_clause
from app.core.rules import normalize_asset_tag, normalize_place, normalize_serial
from app.core.schemas import DownloadLink
from app.core.timeutil import business_datetime

from ..schemas import AssetPatch, CorrectBody, MarkFoundBody, MarkMissingBody
from .common import csv_bytes, stamp, store_export

_MAC = re.compile(r"^[0-9A-F]{12}$")


@dataclass(frozen=True, slots=True)
class AssetFilters:
    q: str | None = None
    status: Sequence[AssetStatus] | None = None
    site_id: Sequence[int | str] | None = None
    location_id: Sequence[int | str] | None = None
    location_type: Sequence[LocationType] | None = None
    article_id: Sequence[int | str] | None = None
    category: Sequence[ArticleCategory] | None = None
    service_request_id: int | None = None
    installation_place: str | None = None
    has_installation_place: bool | None = None
    checked_in_from: date | None = None
    checked_in_to: date | None = None
    source: Sequence[AssetSource] | None = None


def _ints(values: Sequence[int | str] | None) -> list[int]:
    return [v for v in values or () if isinstance(v, int)]


def asset_query(filters: AssetFilters) -> Select[Any]:
    location = aliased(StockLocation)
    stmt = select(Asset).join(location, location.id == Asset.location_id)
    if filters.category:
        stmt = stmt.join(Article, Article.id == Asset.article_id).where(
            Article.category.in_(filters.category)
        )
    clause = search_clause(
        filters.q, Asset.serial_number, Asset.asset_tag, Asset.installation_place, Asset.model
    )
    if clause is not None:
        stmt = stmt.where(clause)
    if filters.status:
        stmt = stmt.where(Asset.status.in_(filters.status))
    if site_ids := _ints(filters.site_id):
        stmt = stmt.where(location.site_id.in_(site_ids))
    if location_ids := _ints(filters.location_id):
        stmt = stmt.where(Asset.location_id.in_(location_ids))
    if filters.location_type:
        stmt = stmt.where(location.type.in_(filters.location_type))
    if article_ids := _ints(filters.article_id):
        stmt = stmt.where(Asset.article_id.in_(article_ids))
    if filters.service_request_id is not None:
        stmt = stmt.where(Asset.service_request_id == filters.service_request_id)
    if filters.installation_place:
        place = normalize_place(filters.installation_place) or filters.installation_place.upper()
        stmt = stmt.where(Asset.installation_place == place)
    if filters.has_installation_place is not None:
        stmt = stmt.where(
            Asset.installation_place.is_not(None)
            if filters.has_installation_place
            else Asset.installation_place.is_(None)
        )
    if filters.checked_in_from is not None:
        stmt = stmt.where(Asset.checked_in_at >= business_datetime(filters.checked_in_from))
    if filters.checked_in_to is not None:
        end = business_datetime(filters.checked_in_to + timedelta(days=1))
        stmt = stmt.where(Asset.checked_in_at < end)
    if filters.source:
        stmt = stmt.where(Asset.source.in_(filters.source))
    return stmt


async def get_asset(session: AsyncSession, asset_id: int) -> Asset:
    asset = await session.get(Asset, asset_id)
    if asset is None:
        raise NotFoundError(f"Asset {asset_id} not found.")
    return asset


async def asset_by_serial(session: AsyncSession, serial: str) -> Asset:
    value = normalize_serial(serial)
    asset = None
    if value:
        asset = (
            await session.scalars(select(Asset).where(Asset.serial_number == value))
        ).first() or (await session.scalars(select(Asset).where(Asset.asset_tag == value))).first()
    if asset is None:
        raise NotFoundError(f"No asset with serial '{serial}'.")
    return asset


def normalize_mac(value: str) -> str | None:
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", value).upper()
    if not _MAC.match(cleaned):
        return None
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


async def patch_asset(session: AsyncSession, asset: Asset, body: AssetPatch) -> Asset:
    fields = body.model_fields_set
    errors: dict[str, list[str]] = {}
    if "asset_tag" in fields:
        tag = normalize_asset_tag(body.asset_tag)
        if tag is not None and tag != asset.asset_tag:
            clash = (
                await session.scalars(
                    select(Asset.id).where(Asset.asset_tag == tag, Asset.id != asset.id)
                )
            ).first()
            if clash is not None:
                raise ConflictError(f"Asset tag {tag} is already used by another asset.")
        asset.asset_tag = tag
    if "mac_address" in fields:
        if body.mac_address:
            mac = normalize_mac(body.mac_address)
            if mac is None:
                errors["macAddress"] = ["Use the format AA:BB:CC:DD:EE:FF."]
            else:
                asset.mac_address = mac
        else:
            asset.mac_address = None
    if "external_asset_id" in fields:
        asset.external_asset_id = (body.external_asset_id or "").strip() or None
    if "ci_name" in fields:
        asset.ci_name = (body.ci_name or "").strip() or None
    if errors:
        raise ValidationProblem(errors)
    await session.flush()
    return asset


EXPORT_HEADER = (
    "Serial",
    "Asset tag",
    "Article number",
    "Article",
    "Model",
    "Status",
    "Location",
    "Installation place",
    "SR number",
    "Checked in",
)


async def export_assets(
    session: AsyncSession, filters: AssetFilters, sort: Sequence[Any], actor: User
) -> DownloadLink:
    stmt = asset_query(filters).order_by(*sort)
    assets = (await session.scalars(stmt)).all()
    rows = [
        [
            asset.serial_number,
            asset.asset_tag,
            asset.article.number if asset.article else None,
            asset.article.name if asset.article else None,
            asset.model,
            label(asset.status),
            asset.location.name if asset.location else None,
            asset.installation_place,
            asset.service_request.number if asset.service_request else None,
            asset.checked_in_at,
        ]
        for asset in assets
    ]
    return await store_export(
        session, csv_bytes(EXPORT_HEADER, rows), f"assets-{stamp()}.csv", actor=actor
    )


# --- action handlers (registered on POST /assets/{id}/actions/{action} + bulk) ---------------


async def _location(
    session: AsyncSession, location_id: int, field: str = "locationId"
) -> StockLocation:
    location = await session.get(StockLocation, location_id)
    if location is None or not location.is_active:
        raise ValidationProblem({field: ["Unknown stock location."]})
    return location


def _place_value(location: StockLocation, raw: str | None) -> str | None:
    if not raw:
        return None
    place = normalize_place(raw)
    if place is None:
        raise ValidationProblem({"installationPlace": ["Use the format B4-12 (shelf-slot)."]})
    if location.type != LocationType.STAGING:
        raise ValidationProblem(
            {"installationPlace": ["Installation places exist only in staging locations."]}
        )
    return place


async def mark_missing(session: AsyncSession, asset: Asset, actor: User, payload: Any) -> None:
    assert isinstance(payload, MarkMissingBody)
    reason = (payload.reason or payload.comment or "").strip() or None
    await asset_service.move(
        session, asset, AssetAction.MARK_MISSING, actor=actor, trigger=Trigger.USER, reason=reason
    )


async def mark_found(session: AsyncSession, asset: Asset, actor: User, payload: Any) -> None:
    assert isinstance(payload, MarkFoundBody)
    location = await _location(session, payload.location_id)
    place = _place_value(location, payload.installation_place)
    await asset_service.move(
        session,
        asset,
        AssetAction.MARK_FOUND,
        actor=actor,
        trigger=Trigger.USER,
        to_location=location,
        installation_place=place,
        reason=(payload.comment or "").strip() or None,
    )


async def correct(session: AsyncSession, asset: Asset, actor: User, payload: Any) -> None:
    assert isinstance(payload, CorrectBody)
    location = (
        await _location(session, payload.location_id) if payload.location_id is not None else None
    )
    place: Any = UNSET
    if "installation_place" in payload.model_fields_set:
        target = location or asset.location
        place = _place_value(target, payload.installation_place)
    service_request: Any = UNSET
    await asset_service.move(
        session,
        asset,
        AssetAction.CORRECT,
        actor=actor,
        trigger=Trigger.USER,
        to_status=payload.status,
        to_location=location,
        installation_place=place,
        service_request=service_request,
        reason=payload.reason,
    )
