"""Per-job state shared by the appliers: lookups, row effects and running counts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Gender, ImportType
from app.core.models import Article, Asset, Contact, User

from .issues import Issue, Row

type Outcome = Literal["created", "updated", "unchanged", "skipped"]


@dataclass(slots=True)
class RowEffects:
    """What one applied row did (merged into :class:`JobStats` only if the row commits)."""

    contacts_created: set[str] = field(default_factory=set)
    contacts_updated: set[str] = field(default_factory=set)
    service_request_ids: set[int] = field(default_factory=set)  # created/updated SRs
    lease_ids: set[int] = field(default_factory=set)  # every lease row of the file
    reopen_lease_ids: set[int] = field(default_factory=set)
    changed_contact_ids: set[int] = field(default_factory=set)
    leases_linked: int = 0
    cancelled_by_itsm: int = 0
    hold_candidates: int = 0
    warnings: list[Issue] = field(default_factory=list)

    def warn(self, code: str, message: str, column: str | None = None, raw: Any = None) -> None:
        self.warnings.append(Issue.warning(code, message, column, raw))


@dataclass(slots=True)
class JobStats:
    processed: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: int = 0
    warnings: int = 0
    contacts_created: set[str] = field(default_factory=set)
    contacts_updated: set[str] = field(default_factory=set)
    service_request_ids: set[int] = field(default_factory=set)
    lease_ids: set[int] = field(default_factory=set)
    reopen_lease_ids: set[int] = field(default_factory=set)
    changed_contact_ids: set[int] = field(default_factory=set)
    leases_linked: int = 0
    cancelled_by_itsm: int = 0
    hold_candidates: int = 0

    def add_row(self, row: Row, outcome: Outcome | None, effects: RowEffects | None) -> None:
        self.processed += 1
        if row.has_errors or outcome is None:
            self.errors += 1
        else:
            setattr(self, outcome, getattr(self, outcome) + 1)
        if row.has_warnings:
            self.warnings += 1
        if effects is not None and not row.has_errors:
            self.contacts_created |= effects.contacts_created
            self.contacts_updated |= effects.contacts_updated - self.contacts_created
            self.service_request_ids |= effects.service_request_ids
            self.lease_ids |= effects.lease_ids
            self.reopen_lease_ids |= effects.reopen_lease_ids
            self.changed_contact_ids |= effects.changed_contact_ids
            self.leases_linked += effects.leases_linked
            self.cancelled_by_itsm += effects.cancelled_by_itsm
            self.hold_candidates += effects.hold_candidates

    def counts(self, total: int | None) -> dict[str, Any]:
        """API ``ImportCounts``."""
        return {
            "total": total,
            "processed": self.processed,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass(slots=True)
class ApplyContext:
    """Job-wide settings and caches; ``session`` is swapped per chunk."""

    import_job_id: int
    import_type: ImportType
    options: dict[str, Any]
    actor_id: int
    now: datetime
    session: AsyncSession = field(init=False)
    _article_by_type: dict[str, int | None] = field(default_factory=dict)
    _article_by_model: dict[str, int | None] = field(default_factory=dict)
    _contact_ids: dict[str, int] = field(default_factory=dict)

    @property
    def source_ref(self) -> str:
        return f"import:{self.import_job_id}"

    @property
    def dry_run(self) -> bool:
        return bool(self.options.get("dryRun"))

    async def actor(self) -> User | None:
        return await self.session.get(User, self.actor_id)

    # --- lookups (ids cached per job, objects fetched per session) -------------------------

    async def article_for_type(self, requested_type: str) -> Article | None:
        """Active article whose internal description matches ``requested_type``."""
        key = requested_type.strip().lower()
        if key not in self._article_by_type:
            self._article_by_type[key] = await self.session.scalar(
                select(Article.id).where(
                    func.lower(Article.internal_description) == key, Article.is_active
                )
            )
        article_id = self._article_by_type[key]
        return await self.session.get(Article, article_id) if article_id else None

    async def article_for_model(self, model: str) -> Article | None:
        """Article by model number, case-insensitive (active or not)."""
        key = model.strip().lower()
        if key not in self._article_by_model:
            self._article_by_model[key] = await self.session.scalar(
                select(Article.id)
                .where(func.lower(Article.model_number) == key)
                .order_by(Article.is_active.desc(), Article.id)
                .limit(1)
            )
        article_id = self._article_by_model[key]
        return await self.session.get(Article, article_id) if article_id else None

    async def asset_by_tag(self, tag: str) -> Asset | None:
        return (await self.session.scalars(select(Asset).where(Asset.asset_tag == tag))).first()

    async def asset_by_serial(self, serial: str) -> Asset | None:
        return (
            await self.session.scalars(select(Asset).where(Asset.serial_number == serial))
        ).first()

    async def contact(self, user_id: str) -> Contact | None:
        contact_id = self._contact_ids.get(user_id)
        if contact_id is not None:
            found = await self.session.get(Contact, contact_id)
            if found is not None:
                return found
        found = (
            await self.session.scalars(select(Contact).where(Contact.user_id == user_id))
        ).first()
        if found is not None:
            self._contact_ids[user_id] = found.id
        return found

    async def upsert_contact(
        self, user_id: str, values: dict[str, Any], effects: RowEffects
    ) -> tuple[Contact, bool]:
        """Create or update a contact; empty values never overwrite (SPEC §7.1, D-20).

        Returns ``(contact, changed)``.
        """
        fields = {k: v for k, v in values.items() if v is not None}
        contact = await self.contact(user_id)
        if contact is None:
            contact = Contact(
                user_id=user_id,
                gender=fields.pop("gender", Gender.UNSPECIFIED),
                last_name=fields.pop("last_name", user_id),
                **fields,
            )
            self.session.add(contact)
            await self.session.flush()
            self._contact_ids[user_id] = contact.id
            effects.contacts_created.add(user_id)
            effects.changed_contact_ids.add(contact.id)
            return contact, True
        changed = False
        for name, value in fields.items():
            if getattr(contact, name) != value:
                setattr(contact, name, value)
                changed = True
        if changed:
            effects.contacts_updated.add(user_id)
            effects.changed_contact_ids.add(contact.id)
        return contact, changed

    def forget_contact(self, user_id: str) -> None:
        """After a row rollback a just-created contact no longer exists."""
        self._contact_ids.pop(user_id, None)


def assign_changes(entity: Any, values: dict[str, Any]) -> set[str]:
    """Set non-``None`` values that differ; returns the changed attribute names."""
    changed: set[str] = set()
    for name, value in values.items():
        if value is None:
            continue
        if getattr(entity, name) != value:
            setattr(entity, name, value)
            changed.add(name)
    return changed
