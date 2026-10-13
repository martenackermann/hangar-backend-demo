"""VIP list (API.md §5.4). Every change recomputes ``is_vip`` via core ``recompute_vip``."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import SR_NON_TERMINAL, LeaseReturnStatus
from app.core.errors import ConflictError, NotFoundError, ValidationProblem
from app.core.models import Contact, LeaseReturn, ServiceRequest, VipEntry
from app.core.pagination import escape_like
from app.core.read_models.refs import contact_ref
from app.core.services import recompute_vip

from ..schemas import VipEntryInput, VipEntryOut, VipEntryPatch


async def build_vip_entries(
    session: AsyncSession, entries: Sequence[VipEntry]
) -> list[VipEntryOut]:
    user_ids = [e.user_id for e in entries]
    contacts: dict[str, Contact] = {}
    srs: dict[str, int] = {}
    leases: dict[str, int] = {}
    if user_ids:
        contacts = {
            c.user_id: c
            for c in (
                await session.scalars(select(Contact).where(Contact.user_id.in_(user_ids)))
            ).all()
        }
        sr_rows = await session.execute(
            select(Contact.user_id, func.count())
            .join(ServiceRequest, ServiceRequest.contact_id == Contact.id)
            .where(Contact.user_id.in_(user_ids), ServiceRequest.status.in_(SR_NON_TERMINAL))
            .group_by(Contact.user_id)
        )
        srs = {u: int(n) for u, n in sr_rows.all()}
        lease_rows = await session.execute(
            select(LeaseReturn.user_id, func.count())
            .where(
                LeaseReturn.user_id.in_(user_ids),
                LeaseReturn.status != LeaseReturnStatus.CLOSED,
            )
            .group_by(LeaseReturn.user_id)
        )
        leases = {u: int(n) for u, n in lease_rows.all()}
    return [
        VipEntryOut(
            id=e.id,
            user_id=e.user_id,
            first_name=e.first_name,
            last_name=e.last_name,
            vip_type=e.vip_type,
            title=e.title,
            note=e.note,
            contact=contact_ref(contacts.get(e.user_id)),
            open_service_requests=srs.get(e.user_id, 0),
            open_lease_returns=leases.get(e.user_id, 0),
            created_at=e.created_at,
        )
        for e in entries
    ]


async def list_entries(session: AsyncSession, q: str | None) -> list[VipEntry]:
    stmt = select(VipEntry).order_by(VipEntry.last_name, VipEntry.first_name, VipEntry.id)
    if q and q.strip():
        pattern = f"%{escape_like(q.strip())}%"
        stmt = stmt.where(
            or_(
                VipEntry.user_id.ilike(pattern, escape="\\"),
                VipEntry.first_name.ilike(pattern, escape="\\"),
                VipEntry.last_name.ilike(pattern, escape="\\"),
                VipEntry.vip_type.ilike(pattern, escape="\\"),
                VipEntry.title.ilike(pattern, escape="\\"),
            )
        )
    return list((await session.scalars(stmt)).all())


async def get_entry(session: AsyncSession, entry_id: int) -> VipEntry:
    entry = await session.get(VipEntry, entry_id)
    if entry is None:
        raise NotFoundError(f"VIP entry {entry_id} not found.")
    return entry


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


async def create_entry(session: AsyncSession, body: VipEntryInput) -> VipEntry:
    user_id = body.user_id.strip().upper()
    if await session.scalar(select(VipEntry.id).where(VipEntry.user_id == user_id)) is not None:
        raise ConflictError(
            f"User {user_id} is already on the VIP list.",
            code="duplicate",
            errors={"userId": ["This user is already on the VIP list."]},
        )
    entry = VipEntry(
        user_id=user_id,
        first_name=_clean(body.first_name),
        last_name=body.last_name.strip(),
        vip_type=body.vip_type.strip(),
        title=_clean(body.title),
        note=_clean(body.note),
    )
    session.add(entry)
    await session.flush()
    await recompute_vip(session, user_id)
    await session.refresh(entry)
    return entry


async def update_entry(session: AsyncSession, entry: VipEntry, body: VipEntryPatch) -> VipEntry:
    values = body.model_dump(exclude_unset=True, by_alias=False)
    for name in ("last_name", "vip_type"):
        if name in values:
            if values[name] is None or not str(values[name]).strip():
                raise ValidationProblem({_camel(name): ["This field cannot be empty."]})
            values[name] = str(values[name]).strip()
    for name in ("first_name", "title", "note"):
        if name in values:
            values[name] = _clean(values[name])
    for name, value in values.items():
        setattr(entry, name, value)
    await session.flush()
    await session.refresh(entry)
    return entry


async def delete_entry(session: AsyncSession, entry: VipEntry) -> None:
    user_id = entry.user_id
    await session.delete(entry)
    await session.flush()
    await recompute_vip(session, user_id)


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)
