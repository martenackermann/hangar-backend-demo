"""Type ``asset_status_sync`` — upsert key ``serial_number`` (SPEC §7.7; RES #49)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.assets import UNSET, asset_service
from app.core.enums import (
    DEACTIVATED,
    AssetAction,
    AssetSource,
    AssetStatus,
    ItemSlot,
    Trigger,
)
from app.core.models import Asset, ServiceRequest

from ..context import ApplyContext, JobStats, Outcome, RowEffects, assign_changes
from ..formats import OLD_DEVICE_STOCKROOM
from ..issues import Row, RowError
from .assets_new import check_tag, resolve_article

_WORKFLOW = frozenset({AssetStatus.ASSIGNED, AssetStatus.PREPARED, AssetStatus.PROVIDED})
_OLD_SLOTS = (ItemSlot.OLD_BASE, ItemSlot.OLD_PERIPHERAL_1, ItemSlot.OLD_PERIPHERAL_2)


def map_state(install_state: str | None, stockroom: str | None) -> AssetAction | None:
    """SPEC §7.7 table; ``None`` = unmapped (row skipped with a warning)."""
    state = (install_state or "").strip().casefold()
    if state == "in use":
        return AssetAction.SYNC_IN_USE
    if state == "in transit":
        return AssetAction.SYNC_COLLECTED
    if state == "in stock" and (stockroom or "").strip().casefold() == (
        OLD_DEVICE_STOCKROOM.casefold()
    ):
        return AssetAction.SYNC_COLLECTED
    return None


def _fields(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_tag": values.get("asset_tag"),
        "model": values.get("model"),
        "order_number": values.get("order_number"),
        "external_asset_id": values.get("external_asset_id"),
        "ci_name": values.get("ci_name"),
        "mac_address": values.get("mac_address"),
        "checked_in_at": values.get("checked_in_date"),
    }


class StatusSyncApplier:
    async def apply(self, ctx: ApplyContext, row: Row, effects: RowEffects) -> Outcome:
        session = ctx.session
        values = row.values
        serial = row.key
        assert serial is not None
        action = map_state(values.get("install_state"), values.get("stockroom"))
        if action is None:
            effects.warn(
                "unmapped_state",
                f"Install state '{values.get('install_state')}'"
                + (f" / stockroom '{values.get('stockroom')}'" if values.get("stockroom") else "")
                + " is not synchronised; the row was skipped.",
                "install_state",
                values.get("install_state"),
            )
            return "skipped"
        asset = await ctx.asset_by_serial(serial)
        if asset is not None:
            if asset.status in _WORKFLOW:
                raise RowError(
                    "asset_in_workflow",
                    f"Serial {serial} is reserved by a service request ({asset.status}).",
                    "serial_number",
                    serial,
                )
            if asset.status in DEACTIVATED:
                raise RowError(
                    "asset_deactivated",
                    f"Serial {serial} has left the fleet ({asset.status}).",
                    "serial_number",
                    serial,
                )
        await check_tag(ctx, values.get("asset_tag"), serial)
        sr: ServiceRequest | None = None
        if values.get("sr_number"):
            sr = (
                await session.scalars(
                    select(ServiceRequest).where(ServiceRequest.number == values["sr_number"])
                )
            ).first()
            if sr is None:
                effects.warn(
                    "sr_not_found",
                    f"Service request {values['sr_number']} is unknown; the device was synchronised without it.",
                    "sr_number",
                    values["sr_number"],
                )
        contact = None
        if action == AssetAction.SYNC_IN_USE and values.get("assigned_user_id"):
            contact = await ctx.contact(values["assigned_user_id"])
            if contact is None:
                effects.warn(
                    "unknown_contact",
                    f"User {values['assigned_user_id']} is not a known contact.",
                    "assigned_user_id",
                    values["assigned_user_id"],
                )
        article = await resolve_article(ctx, values.get("model"), effects)
        actor = await ctx.actor()
        target = AssetStatus.IN_USE if action == AssetAction.SYNC_IN_USE else AssetStatus.COLLECTED

        outcome: Outcome
        if asset is None:
            fields = {k: v for k, v in _fields(values).items() if v is not None}
            asset = Asset(
                serial_number=serial,
                article=article,
                article_id=article.id if article else None,
                source=AssetSource.IMPORT_STATUS_SYNC,
                **fields,
            )
            if contact is not None:
                asset.assigned_contact = contact
                asset.assigned_contact_id = contact.id
            await asset_service.move(
                session,
                asset,
                action,
                actor=actor,
                trigger=Trigger.IMPORT,
                service_request=sr if sr is not None else UNSET,
                source_ref=ctx.source_ref,
            )
            outcome = "created"
        else:
            changed = assign_changes(asset, _fields(values))
            if article is not None and asset.article_id != article.id:
                asset.article, asset.article_id = article, article.id
                changed.add("article_id")
            if contact is not None and asset.assigned_contact_id != contact.id:
                asset.assigned_contact, asset.assigned_contact_id = contact, contact.id
                changed.add("assigned_contact_id")
            if asset.status != target:
                await asset_service.move(
                    session,
                    asset,
                    action,
                    actor=actor,
                    trigger=Trigger.IMPORT,
                    service_request=sr if sr is not None else UNSET,
                    source_ref=ctx.source_ref,
                )
                changed.add("status")
            outcome = "updated" if changed else "unchanged"
        if sr is not None and target == AssetStatus.COLLECTED and asset.asset_tag:
            for item in sr.items:
                if (
                    item.slot in _OLD_SLOTS
                    and item.expected_asset_tag == asset.asset_tag
                    and item.asset_id is None
                ):
                    item.asset, item.asset_id = asset, asset.id
                    if outcome == "unchanged":
                        outcome = "updated"
        return outcome

    async def post_process(
        self, ctx: ApplyContext, stats: JobStats, *, seen_keys: set[str], complete: bool
    ) -> dict[str, int]:
        return {}
