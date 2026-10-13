"""Type ``contacts`` — upsert key ``user_id`` (SPEC §7.5)."""

from __future__ import annotations

from sqlalchemy import select

from app.core.enums import LeaseReturnStatus
from app.core.models import LeaseReturn
from app.core.region_resolver import apply_lease_campus

from ..context import ApplyContext, JobStats, Outcome, RowEffects
from ..issues import Row

FIELDS = (
    "gender",
    "first_name",
    "last_name",
    "email",
    "phone",
    "mobile",
    "department",
    "street",
    "zip",
    "city",
    "building",
    "room",
)


class ContactsApplier:
    async def apply(self, ctx: ApplyContext, row: Row, effects: RowEffects) -> Outcome:
        user_id = row.key
        assert user_id is not None
        created = await ctx.contact(user_id) is None
        contact, changed = await ctx.upsert_contact(
            user_id, {name: row.values.get(name) for name in FIELDS}, effects
        )
        if contact.email is None:
            effects.warn("email_missing", "The contact has no email address.", "email")
        if created:
            return "created"
        return "updated" if changed else "unchanged"

    async def post_process(
        self, ctx: ApplyContext, stats: JobStats, *, seen_keys: set[str], complete: bool
    ) -> dict[str, int]:
        """Recompute ``is_campus`` of open lease returns of changed contacts (SPEC §7.5)."""
        if not ctx.dry_run and stats.changed_contact_ids:
            leases = (
                await ctx.session.scalars(
                    select(LeaseReturn).where(
                        LeaseReturn.contact_id.in_(stats.changed_contact_ids),
                        LeaseReturn.status != LeaseReturnStatus.CLOSED,
                    )
                )
            ).all()
            for lease in leases:
                await apply_lease_campus(ctx.session, lease)
        return {
            "contactsCreated": len(stats.contacts_created),
            "contactsUpdated": len(stats.contacts_updated),
        }
