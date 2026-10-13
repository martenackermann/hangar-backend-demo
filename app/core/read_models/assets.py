"""Asset response models + builders (API.md §8.3)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actions import actions
from app.core.enums import AssetAction, AssetSource, AssetStatus, LeaseReturnStatus, Trigger, label
from app.core.models import Asset, AssetMovement, LeaseReturn, User
from app.core.read_models.refs import (
    asset_summary,
    contact_ref,
    location_ref,
    site_ref,
    sr_ref,
    user_ref,
)
from app.core.schemas import (
    ApiModel,
    AssetSummary,
    BlockedAction,
    ContactRef,
    LocationRef,
    SiteRef,
    SrRef,
    UserRef,
)


class AssetListItem(AssetSummary):
    site: SiteRef | None
    assigned_contact: ContactRef | None
    checked_in_at: datetime | None
    status_changed_at: datetime
    source: AssetSource
    available_actions: list[str]


class AssetLeaseRef(ApiModel):
    id: int
    status: LeaseReturnStatus
    retirement_date: date


class AssetMovementOut(ApiModel):
    id: int
    action: AssetAction
    action_label: str
    from_status: AssetStatus | None
    to_status: AssetStatus
    from_location: LocationRef | None
    to_location: LocationRef
    from_installation_place: str | None
    to_installation_place: str | None
    service_request: SrRef | None
    actor: UserRef | None
    trigger: Trigger
    reason: str | None
    source_ref: str | None
    occurred_at: datetime


class AssetDetail(AssetListItem):
    version: int
    external_asset_id: str | None
    ci_name: str | None
    order_number: str | None
    mac_address: str | None
    lease_return: AssetLeaseRef | None
    recent_movements: list[AssetMovementOut]
    blocked_actions: list[BlockedAction]


def build_movement(movement: AssetMovement) -> AssetMovementOut:
    to_location = location_ref(movement.to_location)
    assert to_location is not None
    return AssetMovementOut(
        id=movement.id,
        action=movement.action,
        action_label=label(movement.action),
        from_status=movement.from_status,
        to_status=movement.to_status,
        from_location=location_ref(movement.from_location),
        to_location=to_location,
        from_installation_place=movement.from_installation_place,
        to_installation_place=movement.to_installation_place,
        service_request=sr_ref(movement.service_request),
        actor=user_ref(movement.actor),
        trigger=movement.trigger,
        reason=movement.reason,
        source_ref=movement.source_ref,
        occurred_at=movement.occurred_at,
    )


def _list_fields(asset: Asset, available: list[str]) -> dict[str, object]:
    summary = asset_summary(asset)
    assert summary is not None
    return {
        **summary.model_dump(by_alias=False),
        "article": summary.article,
        "location": summary.location,
        "service_request": summary.service_request,
        "site": site_ref(asset.location.site if asset.location else None),
        "assigned_contact": contact_ref(asset.assigned_contact),
        "checked_in_at": asset.checked_in_at,
        "status_changed_at": asset.status_changed_at,
        "source": asset.source,
        "available_actions": available,
    }


async def build_asset_list_items(
    session: AsyncSession, assets: Sequence[Asset], user: User | None
) -> list[AssetListItem]:
    return [
        AssetListItem(**_list_fields(asset, await actions.available_names(session, asset, user)))  # type: ignore[arg-type]
        for asset in assets
    ]


async def build_asset_detail(session: AsyncSession, asset: Asset, user: User | None) -> AssetDetail:
    available, blocked = await actions.available(session, asset, user)
    movements = (
        await session.scalars(
            select(AssetMovement)
            .where(AssetMovement.asset_id == asset.id)
            .order_by(AssetMovement.occurred_at.desc(), AssetMovement.id.desc())
            .limit(20)
        )
    ).all()
    lease = None
    if asset.asset_tag:
        lease = (
            await session.scalars(
                select(LeaseReturn).where(LeaseReturn.asset_tag == asset.asset_tag)
            )
        ).first()
    return AssetDetail(
        **_list_fields(asset, available),  # type: ignore[arg-type]
        version=asset.version,
        external_asset_id=asset.external_asset_id,
        ci_name=asset.ci_name,
        order_number=asset.order_number,
        mac_address=asset.mac_address,
        lease_return=AssetLeaseRef(
            id=lease.id, status=lease.status, retirement_date=lease.retirement_date
        )
        if lease
        else None,
        recent_movements=[build_movement(m) for m in movements],
        blocked_actions=blocked,
    )
