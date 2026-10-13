"""Type ``service_requests`` — upsert key ``sr_number`` (SPEC §7.4, §6.4, §6.5; RES #29/#37)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.enums import (
    REGION_MUTABLE_STATUSES,
    SR_TERMINAL,
    HoldSource,
    ItemSlot,
    LeaseReturnStatus,
    ServiceRequestStatus,
    Trigger,
)
from app.core.models import LeaseReturn, ServiceRequest, ServiceRequestItem
from app.core.region_resolver import apply_region
from app.core.services import is_vip_user, link_lease_returns, recompute_vip
from app.core.transitions import LEASE_TRANSITIONS, SR_TRANSITIONS, transition_service

from ..context import ApplyContext, JobStats, Outcome, RowEffects, assign_changes
from ..issues import Row

SR = ServiceRequestStatus

NEW_SLOTS: dict[ItemSlot, tuple[str, str]] = {
    ItemSlot.NEW_BASE: ("new_base", "New base device type"),
    ItemSlot.NEW_PERIPHERAL_1: ("new_peripheral_1", "New peripheral 1 type"),
    ItemSlot.NEW_PERIPHERAL_2: ("new_peripheral_2", "New peripheral 2 type"),
}
OLD_SLOTS: dict[ItemSlot, tuple[str, str]] = {
    ItemSlot.OLD_BASE: ("old_base", "Old base device"),
    ItemSlot.OLD_PERIPHERAL_1: ("old_peripheral_1", "Old peripheral 1"),
    ItemSlot.OLD_PERIPHERAL_2: ("old_peripheral_2", "Old peripheral 2"),
}
_REGION_INPUTS = frozenset(
    {
        "delivery_street",
        "delivery_zip",
        "delivery_city",
        "delivery_country",
        "cost_center",
        "procurement_type",
    }
)
_HOLD = SR_TRANSITIONS.get("mark_hardware_unavailable")
_LINK = LEASE_TRANSITIONS.get("link_service_request")
assert _HOLD is not None
assert _LINK is not None
HOLD_FROM = _HOLD.from_
LINKABLE = frozenset(LeaseReturnStatus(s) for s in _LINK.from_)
ITSM_CANCELLED = "Cancelled"


def is_hold_group(group: str | None) -> bool:
    target = get_settings().itsm_hold_assignment_group.strip().casefold()
    return bool(group) and (group or "").strip().casefold() == target


def _fields(values: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = values.get("order_details") or {}
    address: dict[str, Any] = values.get("delivery_address") or {}
    software = details.get("requested_software")
    return {
        "task_number": values.get("task_number"),
        "itsm_state": values.get("itsm_state"),
        "assignment_group": values.get("assignment_group"),
        "opened_at": values.get("opened_at"),
        "catalog_item": values.get("catalog_item"),
        "short_description": values.get("short_description"),
        "cost_center": values.get("cost_center"),
        "procurement_type": details.get("procurement_type"),
        "scope_of_delivery": details.get("scope_of_delivery"),
        "client_component": details.get("client_component"),
        "requested_software": software or None,
        "note": details.get("note"),
        "onsite_contact_phone": details.get("onsite_contact_phone"),
        "delivery_room": address.get("room"),
        "delivery_building": address.get("building"),
        "delivery_street": address.get("street"),
        "delivery_zip": address.get("zip"),
        "delivery_city": address.get("city"),
        "delivery_country": address.get("country"),
    }


def _contact_values(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "gender": values.get("gender"),
        "first_name": values.get("first_name"),
        "last_name": values.get("last_name"),
        "email": values.get("email"),
    }


class ServiceRequestsApplier:
    async def apply(self, ctx: ApplyContext, row: Row, effects: RowEffects) -> Outcome:
        session = ctx.session
        values = row.values
        number = row.key
        assert number is not None
        contact, contact_changed = await ctx.upsert_contact(
            values["user_id"], _contact_values(values), effects
        )
        if contact.email is None:
            effects.warn(
                "email_missing",
                "The contact has no email address; automatic mails cannot be sent.",
                "email",
            )
        details: dict[str, Any] = values.get("order_details") or {}
        desired_new = {
            slot: details[key] for slot, (key, _) in NEW_SLOTS.items() if details.get(key)
        }
        desired_old = {
            slot: details[key] for slot, (key, _) in OLD_SLOTS.items() if details.get(key)
        }
        sr = (
            await session.scalars(select(ServiceRequest).where(ServiceRequest.number == number))
        ).first()
        if sr is None:
            return await self._create(ctx, values, contact, desired_new, desired_old, effects)
        return await self._update(
            ctx, sr, values, contact, contact_changed, desired_new, desired_old, effects
        )

    # --- create -------------------------------------------------------------------------------

    async def _create(
        self,
        ctx: ApplyContext,
        values: dict[str, Any],
        contact: Any,
        desired_new: dict[ItemSlot, str],
        desired_old: dict[ItemSlot, str],
        effects: RowEffects,
    ) -> Outcome:
        session = ctx.session
        fields = {k: v for k, v in _fields(values).items() if v is not None}
        fields.setdefault("delivery_country", "Freeland")
        # Built transient (not yet in the session) so lookups cannot autoflush a status-less row.
        sr = ServiceRequest(
            number=values["sr_number"],
            contact=contact,
            contact_id=contact.id,
            imported_at=ctx.now,
            last_imported_at=ctx.now,
            last_import_job_id=ctx.import_job_id,
            items=[],
            **fields,
        )
        for slot, requested in desired_new.items():
            sr.items.append(await self._new_item(ctx, slot, requested, effects))
        for slot, tag in desired_old.items():
            asset = await ctx.asset_by_tag(tag)
            sr.items.append(
                ServiceRequestItem(
                    slot=slot,
                    expected_asset_tag=tag,
                    asset=asset,
                    asset_id=asset.id if asset else None,
                )
            )
        sr.is_vip = await is_vip_user(session, contact.user_id)
        await apply_region(session, sr)
        if sr.region_id is None:
            effects.warn(
                "region_unresolved",
                "No region rule matched the delivery address; the request stays unresolved.",
                "delivery_address",
            )
        actor = await ctx.actor()  # before add(): a lookup query would autoflush the new row
        session.add(sr)
        await transition_service.apply(
            session, sr, "import_create", actor=actor, trigger=Trigger.IMPORT
        )
        effects.service_request_ids.add(sr.id)
        if sr.itsm_state == ITSM_CANCELLED:
            await self._cancel(ctx, sr, effects)
        else:
            await self._link(ctx, sr, effects)
            if is_hold_group(sr.assignment_group) and sr.status in HOLD_FROM:
                effects.hold_candidates += 1
        return "created"

    async def _new_item(
        self, ctx: ApplyContext, slot: ItemSlot, requested: str, effects: RowEffects
    ) -> ServiceRequestItem:
        article = await ctx.article_for_type(requested)
        if article is None:
            effects.warn(
                "unknown_article",
                f"Device type '{requested}' is not in the article catalogue.",
                f"order_details.{NEW_SLOTS[slot][1]}",
                requested,
            )
        return ServiceRequestItem(
            slot=slot,
            requested_type=requested,
            article=article,
            article_id=article.id if article else None,
        )

    # --- update -------------------------------------------------------------------------------

    async def _update(
        self,
        ctx: ApplyContext,
        sr: ServiceRequest,
        values: dict[str, Any],
        contact: Any,
        contact_changed: bool,
        desired_new: dict[ItemSlot, str],
        desired_old: dict[ItemSlot, str],
        effects: RowEffects,
    ) -> Outcome:
        session = ctx.session
        changed = assign_changes(sr, _fields(values))
        if sr.contact_id != contact.id:
            sr.contact = contact
            sr.contact_id = contact.id
            changed.add("contact_id")
            await session.flush()
            await recompute_vip(session, contact.user_id)
        sr.last_imported_at = ctx.now
        sr.last_import_job_id = ctx.import_job_id
        terminal = sr.status in SR_TERMINAL
        mutable = sr.status in REGION_MUTABLE_STATUSES
        if changed & _REGION_INPUTS and not terminal:
            if await apply_region(session, sr):
                changed.add("region")
            if mutable and not sr.region_overridden and sr.region_id is None:
                effects.warn(
                    "region_unresolved",
                    "No region rule matched the delivery address; the request stays unresolved.",
                    "delivery_address",
                )
        item_changes = await self._sync_items(ctx, sr, desired_new, desired_old, effects, mutable)
        if item_changes and not mutable:
            effects.warn(
                "items_locked",
                f"Item changes were ignored because the request is already "
                f"'{sr.status}' ({', '.join(sorted(item_changes))}).",
                "order_details",
            )
        elif item_changes:
            changed |= item_changes
        if not terminal:
            if sr.itsm_state == ITSM_CANCELLED:
                await self._cancel(ctx, sr, effects)
                changed.add("status")
            else:
                if await self._link(ctx, sr, effects):
                    changed.add("lease_link")
                if is_hold_group(sr.assignment_group) and sr.status in HOLD_FROM and changed:
                    effects.hold_candidates += 1
        if changed or contact_changed:
            effects.service_request_ids.add(sr.id)
            return "updated"
        return "unchanged"

    async def _sync_items(
        self,
        ctx: ApplyContext,
        sr: ServiceRequest,
        desired_new: dict[ItemSlot, str],
        desired_old: dict[ItemSlot, str],
        effects: RowEffects,
        apply: bool,
    ) -> set[str]:
        """Re-sync items (SPEC §7.4 "Update"); returns the changed slots."""
        changes: set[str] = set()
        for slot in NEW_SLOTS:
            item = sr.item(slot)
            wanted = desired_new.get(slot)
            if wanted is None:
                if item is not None and item.asset_id is None:
                    changes.add(slot.value)
                    if apply:
                        sr.items.remove(item)
                continue
            if item is None:
                changes.add(slot.value)
                if apply:
                    sr.items.append(await self._new_item(ctx, slot, wanted, effects))
                continue
            if (item.requested_type or "").casefold() != wanted.casefold():
                changes.add(slot.value)
                if apply:
                    replacement = await self._new_item(ctx, slot, wanted, effects)
                    item.requested_type = wanted
                    item.article = replacement.article
                    item.article_id = replacement.article_id
            elif apply and item.article_id is None:
                article = await ctx.article_for_type(wanted)
                if article is not None:
                    item.article, item.article_id = article, article.id
                    changes.add(slot.value)
        for slot in OLD_SLOTS:
            item = sr.item(slot)
            wanted = desired_old.get(slot)
            if wanted is None:
                if item is not None and item.asset_id is None:
                    changes.add(slot.value)
                    if apply:
                        sr.items.remove(item)
                continue
            if item is None:
                changes.add(slot.value)
                if apply:
                    asset = await ctx.asset_by_tag(wanted)
                    sr.items.append(
                        ServiceRequestItem(
                            slot=slot,
                            expected_asset_tag=wanted,
                            asset=asset,
                            asset_id=asset.id if asset else None,
                        )
                    )
                continue
            if item.expected_asset_tag != wanted and item.verified_at is None:
                changes.add(slot.value)
                if apply:
                    asset = await ctx.asset_by_tag(wanted)
                    item.expected_asset_tag = wanted
                    item.asset = asset
                    item.asset_id = asset.id if asset else None
        return changes

    # --- effects ------------------------------------------------------------------------------

    async def _link(self, ctx: ApplyContext, sr: ServiceRequest, effects: RowEffects) -> bool:
        """SPEC §6.4: link lease returns named by old items (create **and** update)."""
        tags = [i.expected_asset_tag for i in sr.old_items if i.expected_asset_tag]
        if not tags:
            return False
        session = ctx.session
        linkable = int(
            await session.scalar(
                select(func.count())
                .select_from(LeaseReturn)
                .where(LeaseReturn.asset_tag.in_(tags), LeaseReturn.status.in_(LINKABLE))
            )
            or 0
        )
        warnings = await link_lease_returns(session, sr, Trigger.IMPORT, await ctx.actor())
        for warning in warnings:
            effects.warn(warning.code, warning.message, "order_details", warning.asset_tag)
        effects.leases_linked += linkable
        return linkable > 0

    async def _cancel(self, ctx: ApplyContext, sr: ServiceRequest, effects: RowEffects) -> None:
        """SPEC §6.5 / D-21: ITSM state ``Cancelled`` cancels non-terminal requests."""
        await transition_service.apply(
            ctx.session,
            sr,
            "cancel",
            actor=await ctx.actor(),
            trigger=Trigger.IMPORT,
            comment="Cancelled in ITSM",
        )
        effects.cancelled_by_itsm += 1

    # --- post-processing ----------------------------------------------------------------------

    async def post_process(
        self, ctx: ApplyContext, stats: JobStats, *, seen_keys: set[str], complete: bool
    ) -> dict[str, int]:
        """SPEC §6.5: requests of this import in the hold group go to ``hardware_unavailable``."""
        hold = 0
        if ctx.dry_run:
            hold = stats.hold_candidates
        elif stats.service_request_ids:
            session = ctx.session
            actor = await ctx.actor()
            candidates = (
                await session.scalars(
                    select(ServiceRequest)
                    .where(
                        ServiceRequest.id.in_(stats.service_request_ids),
                        ServiceRequest.status.in_(HOLD_FROM),
                    )
                    .order_by(ServiceRequest.id)
                )
            ).all()
            for sr in candidates:
                if not is_hold_group(sr.assignment_group):
                    continue
                await transition_service.apply(
                    session, sr, "mark_hardware_unavailable", actor=actor, trigger=Trigger.IMPORT
                )
                sr.hold_source = HoldSource.IMPORT  # SPEC_RESOLUTIONS #37
                hold += 1
        return {
            "hardwareHold": hold,
            "cancelledByItsm": stats.cancelled_by_itsm,
            "leaseReturnsLinked": stats.leases_linked,
            "contactsCreated": len(stats.contacts_created),
            "contactsUpdated": len(stats.contacts_updated),
        }
