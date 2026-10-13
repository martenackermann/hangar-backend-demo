"""Type ``lease_returns`` — upsert key ``asset_tag`` (SPEC §7.3; SPEC_RESOLUTIONS #55)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.enums import SR_TERMINAL, LeaseReturnStatus, Trigger
from app.core.models import LeaseReturn, ServiceRequest, ServiceRequestItem
from app.core.region_resolver import apply_lease_campus
from app.core.services import is_vip_user, link_lease_return, recompute_vip
from app.core.transitions import LEASE_TRANSITIONS, transition_service

from ..context import ApplyContext, JobStats, Outcome, RowEffects, assign_changes
from ..issues import Row

LR = LeaseReturnStatus
_LINK = LEASE_TRANSITIONS.get("link_service_request")
_MISSING = LEASE_TRANSITIONS.get("mark_probably_closed")
assert _LINK is not None
assert _MISSING is not None
LINKABLE = frozenset(LR(s) for s in _LINK.from_)
MARK_MISSING_FROM = frozenset(LR(s) for s in _MISSING.from_)


def _fields(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "serial_number": values.get("serial_number"),
        "model_category": values.get("model_category"),
        "checked_in_at": values.get("checked_in_date"),
        "retirement_date": values.get("retirement_date"),
        "model": values.get("model"),
        "install_status": values.get("install_status"),
        "ci_name": values.get("ci_name"),
        "cost_center": values.get("cost_center"),
        "escalation_email": values.get("escalation_email"),
    }


class LeaseReturnsApplier:
    async def apply(self, ctx: ApplyContext, row: Row, effects: RowEffects) -> Outcome:
        session = ctx.session
        values = row.values
        tag = row.key
        assert tag is not None
        user_id: str = values["user_id"]
        contact, contact_changed = await ctx.upsert_contact(
            user_id,
            {
                "gender": values.get("gender"),
                "first_name": values.get("first_name"),
                "last_name": values.get("last_name"),
                "email": values.get("email"),
            },
            effects,
        )
        if contact.email is None:
            effects.warn(
                "email_missing",
                "The contact has no email address; lease mails cannot be sent.",
                "email",
            )
        lease = (
            await session.scalars(select(LeaseReturn).where(LeaseReturn.asset_tag == tag))
        ).first()
        if lease is None:
            lease = LeaseReturn(
                asset_tag=tag,
                user_id=user_id,
                contact=contact,
                contact_id=contact.id,
                imported_at=ctx.now,
                last_seen_at=ctx.now,
                last_import_job_id=ctx.import_job_id,
                **{k: v for k, v in _fields(values).items() if v is not None},
            )
            lease.is_vip = await is_vip_user(session, user_id)
            await apply_lease_campus(session, lease)
            session.add(lease)
            await transition_service.apply(
                session, lease, "import_create", actor=await ctx.actor(), trigger=Trigger.IMPORT
            )
            effects.lease_ids.add(lease.id)
            return "created"

        changed = assign_changes(lease, _fields(values))
        if lease.user_id != user_id or lease.contact_id != contact.id:
            lease.user_id = user_id
            lease.contact = contact
            lease.contact_id = contact.id
            changed.add("user_id")
            await session.flush()
            await recompute_vip(session, user_id)
        if await apply_lease_campus(session, lease):
            changed.add("is_campus")
        lease.last_seen_at = ctx.now
        lease.last_import_job_id = ctx.import_job_id
        effects.lease_ids.add(lease.id)
        if lease.status == LR.PROBABLY_CLOSED:
            effects.reopen_lease_ids.add(lease.id)
            changed.add("status")
        return "updated" if changed or contact_changed else "unchanged"

    async def post_process(
        self, ctx: ApplyContext, stats: JobStats, *, seen_keys: set[str], complete: bool
    ) -> dict[str, int]:
        session = ctx.session
        actor = await ctx.actor()
        reopened = linked = probably_closed = 0
        # 1. rows found again → reopen (SPEC §7.3)
        if ctx.dry_run:
            reopened = len(stats.reopen_lease_ids)
        else:
            for lease in await self._leases(ctx, stats.reopen_lease_ids):
                if lease.status == LR.PROBABLY_CLOSED:
                    await transition_service.apply(
                        session, lease, "reopen", actor=actor, trigger=Trigger.IMPORT
                    )
                    reopened += 1
        # 2. late lease imports also run linking (SPEC_RESOLUTIONS #55)
        if not ctx.dry_run:
            linked = await self._link_late(ctx, stats.lease_ids)
        # 3. option: open leases missing from this (complete) file → probably closed
        if ctx.options.get("markMissingAsProbablyClosed") and complete:
            missing = (
                await session.scalars(
                    select(LeaseReturn)
                    .where(LeaseReturn.status.in_(MARK_MISSING_FROM))
                    .order_by(LeaseReturn.id)
                )
            ).all()
            for lease in missing:
                if lease.asset_tag in seen_keys:
                    continue
                if not ctx.dry_run:
                    await transition_service.apply(
                        session, lease, "mark_probably_closed", actor=actor, trigger=Trigger.IMPORT
                    )
                probably_closed += 1
        return {
            "reopened": reopened,
            "leaseReturnsLinked": linked,
            "probablyClosed": probably_closed,
            "contactsCreated": len(stats.contacts_created),
            "contactsUpdated": len(stats.contacts_updated),
        }

    async def _leases(self, ctx: ApplyContext, ids: set[int]) -> list[LeaseReturn]:
        if not ids:
            return []
        return list(
            (
                await ctx.session.scalars(
                    select(LeaseReturn).where(LeaseReturn.id.in_(ids)).order_by(LeaseReturn.id)
                )
            ).all()
        )

    async def _link_late(self, ctx: ApplyContext, lease_ids: set[int]) -> int:
        """Link leases of this file to open requests that already name their asset tag."""
        session = ctx.session
        leases = [lease for lease in await self._leases(ctx, lease_ids) if lease.status in LINKABLE]
        if not leases:
            return 0
        by_tag = {lease.asset_tag: lease for lease in leases}
        matches = (
            await session.execute(
                select(ServiceRequestItem.expected_asset_tag, ServiceRequest.id)
                .join(ServiceRequest, ServiceRequest.id == ServiceRequestItem.service_request_id)
                .where(
                    ServiceRequestItem.expected_asset_tag.in_(list(by_tag)),
                    ServiceRequest.status.not_in(SR_TERMINAL),
                )
                .order_by(ServiceRequest.imported_at.desc(), ServiceRequest.id.desc())
            )
        ).all()
        actor = await ctx.actor()
        linked = 0
        done: set[str] = set()
        for tag, sr_id in matches:
            if tag is None or tag in done:
                continue
            done.add(tag)
            sr = await session.get(ServiceRequest, sr_id)
            if sr is None:
                continue
            await link_lease_return(session, by_tag[tag], sr, actor=actor, trigger=Trigger.IMPORT)
            linked += 1
        return linked
