"""Small helpers shared by the H3 services (scan results, SQL expressions, lookups)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, Date, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.constants import SCAN_CODES
from app.core.enums import NEW_SLOTS, DeliveryType, ItemSlot, SiteKind, label
from app.core.errors import ApiError, ConflictError, GuardFailed, NotFoundError
from app.core.loading import ensure_loaded
from app.core.models import Asset, Contact, ServiceRequest, ServiceRequestItem, Site
from app.core.rules import normalize_serial
from app.core.schemas import ScanProgress, ScanResult, UndoLink
from app.core.transitions import load_for_update
from app.core.transitions.guards import is_editor


def business_date(column: Any) -> ColumnElement[Any]:
    """The business-TZ calendar date of a ``timestamptz`` column (SPEC §0)."""
    return cast(func.timezone(get_settings().business_tz, column), Date)


def effective_delivery_type() -> ColumnElement[Any]:
    return func.coalesce(ServiceRequest.delivery_type_override, ServiceRequest.delivery_type)


def is_delivery(*types: DeliveryType) -> ColumnElement[bool]:
    return effective_delivery_type().in_([str(t) for t in types])


def sr_search_clause(term: str | None) -> ColumnElement[bool] | None:
    """``q`` over SR number / task number / contact name, email and user id."""
    if not term or not term.strip():
        return None
    pattern = f"%{term.strip().replace('%', '').replace('_', '')}%"
    contact_match = (
        select(Contact.id)
        .where(
            or_(
                Contact.last_name.ilike(pattern),
                Contact.first_name.ilike(pattern),
                (func.coalesce(Contact.first_name, "") + " " + Contact.last_name).ilike(pattern),
                Contact.email.ilike(pattern),
                Contact.user_id.ilike(pattern),
            )
        )
        .scalar_subquery()
    )
    return or_(
        ServiceRequest.number.ilike(pattern),
        ServiceRequest.task_number.ilike(pattern),
        ServiceRequest.contact_id.in_(contact_match),
    )


def slot_label(slot: ItemSlot | str) -> str:
    return label(ItemSlot(slot))


def scan_result[T](
    code: str,
    message: str,
    scanned_value: str,
    *,
    applied: bool = False,
    entity: T | None = None,
    undo: UndoLink | None = None,
    progress: tuple[int, int] | None = None,
) -> ScanResult[T]:
    """A ``ScanResult`` whose verdict comes from the single core table (SPEC_RESOLUTIONS #25)."""
    return ScanResult(
        verdict=SCAN_CODES[code].verdict,
        code=code,
        message=message,
        scanned_value=scanned_value,
        applied=applied,
        entity=entity,
        undo=undo,
        progress=ScanProgress(done=progress[0], total=progress[1]) if progress else None,
    )


async def find_asset(session: AsyncSession, value: str) -> Asset | None:
    """Asset by serial number, falling back to the asset tag (labels carry both)."""
    normalized = normalize_serial(value)
    if normalized is None:
        return None
    asset = (await session.scalars(select(Asset).where(Asset.serial_number == normalized))).first()
    if asset is None:
        asset = (await session.scalars(select(Asset).where(Asset.asset_tag == normalized))).first()
    return asset


async def get_sr(session: AsyncSession, sr_id: int, *, for_update: bool = False) -> ServiceRequest:
    if for_update:
        sr = await load_for_update(session, ServiceRequest, sr_id)
    else:
        found = await session.get(ServiceRequest, sr_id)
        if found is None:
            raise NotFoundError(f"Service request {sr_id} not found.")
        sr = found
    await ensure_loaded(session, sr)
    return sr


async def get_hub(session: AsyncSession, site_id: int) -> Site:
    site = await session.get(Site, site_id)
    if site is None:
        raise NotFoundError(f"Site {site_id} not found.")
    if site.kind != SiteKind.HUB:
        raise ApiError(
            "Choose a hub site.",
            status=422,
            code="validation_error",
            errors={"siteId": ["Choose a hub site."]},
        )
    return site


def require_new_slot(slot: ItemSlot) -> None:
    """``old_*`` slots never take assignments or packing marks (SPEC_RESOLUTIONS #35)."""
    if slot not in NEW_SLOTS:
        raise ApiError(
            f"Slot '{slot}' collects an old device; only new slots can be assigned or packed.",
            status=422,
            code="slot_not_assignable",
            title="Slot not assignable",
            errors={"slot": ["Only new_* slots can be assigned or packed."]},
        )


def item_for(sr: ServiceRequest, slot: ItemSlot) -> ServiceRequestItem:
    item = sr.item(slot)
    if item is None:
        raise NotFoundError(f"{sr.number} has no item in slot '{slot}'.")
    return item


async def require_editor(session: AsyncSession, sr: ServiceRequest, actor: Any) -> None:
    """``409 guard_failed`` (guard ``is_editor``) unless ``actor`` holds the claim."""
    failure = await is_editor(session, sr, actor, None)
    if failure is not None:
        raise GuardFailed(failure.code, failure.message)


def conflict(detail: str) -> ConflictError:
    return ConflictError(detail, code="conflict")


def new_item_progress(sr: ServiceRequest, attr: str) -> tuple[int, int]:
    items = sr.new_items
    return sum(1 for i in items if getattr(i, attr) is not None), len(items)
