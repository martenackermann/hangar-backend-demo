"""Global search / ⌘K resolution — SPEC §6.19, API.md §3.5.

Core provides the SR, lease return, asset, contact and installation-place providers.
Slices may add providers via ``reg.register_search_provider(fn)`` where
``fn(session, q, user) -> list[SearchHit]``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Literal

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import can
from app.core.enums import AssetStatus, LeaseReturnStatus, ServiceRequestStatus, Tone, label, tone
from app.core.models import Asset, Contact, LeaseReturn, ServiceRequest, User
from app.core.rules import normalize_place
from app.core.schemas import ApiModel

type SearchKind = Literal[
    "service_request", "lease_return", "asset", "contact", "installation_place"
]
type SearchProvider = Callable[[AsyncSession, str, User], Awaitable[list["SearchHit"]]]

KIND_ORDER: tuple[SearchKind, ...] = (
    "service_request",
    "lease_return",
    "asset",
    "contact",
    "installation_place",
)
KIND_LABELS: dict[str, str] = {
    "service_request": "Service requests",
    "lease_return": "Lease returns",
    "asset": "Assets",
    "contact": "Contacts",
    "installation_place": "Installation places",
}
#: Read permission behind each hit kind: hits the caller may not open are dropped.
KIND_PERMISSIONS: dict[str, str] = {
    "service_request": "sr.read",
    "lease_return": "lease.read",
    "asset": "stock.read",
    "contact": "sr.read",
    "installation_place": "stock.read",
}
PER_GROUP = 5
TOTAL = 20
SIMILARITY = 0.3

_SR = re.compile(r"^SR[0-9]{7}$")
_TASK = re.compile(r"^TASK[0-9]{7}$")
_TAG = re.compile(r"^[A-Z]{2}[0-9]{6}$")
_USER_ID = re.compile(r"^[A-Z0-9]{3,16}$")


class SearchHit(ApiModel):
    kind: SearchKind
    id: int | None
    title: str
    subtitle: str | None
    status: str | None
    tone: Tone | None
    href: str
    exact: bool


class SearchGroup(ApiModel):
    kind: SearchKind
    label: str
    hits: list[SearchHit]


class SearchResponse(ApiModel):
    query: str
    exact: SearchHit | None
    groups: list[SearchGroup]


# --- hit builders -----------------------------------------------------------------------------


def sr_hit(sr: ServiceRequest, exact: bool) -> SearchHit:
    parts = [label(sr.status, ServiceRequestStatus)]
    if sr.region is not None:
        parts.append(sr.region.name)
    if sr.contact is not None:
        parts.append(sr.contact.display_name)
    return SearchHit(
        kind="service_request",
        id=sr.id,
        title=sr.number,
        subtitle=" · ".join(parts),
        status=sr.status,
        tone=tone(sr.status, ServiceRequestStatus),
        href=f"/service-requests?sr={sr.id}",
        exact=exact,
    )


def lease_hit(lease: LeaseReturn, exact: bool) -> SearchHit:
    parts = [label(lease.status, LeaseReturnStatus), f"retires {lease.retirement_date.isoformat()}"]
    if lease.contact is not None:
        parts.append(lease.contact.display_name)
    return SearchHit(
        kind="lease_return",
        id=lease.id,
        title=lease.asset_tag,
        subtitle=" · ".join(parts),
        status=lease.status,
        tone=tone(lease.status, LeaseReturnStatus),
        href=f"/lease-returns?lr={lease.id}",
        exact=exact,
    )


def asset_hit(asset: Asset, exact: bool) -> SearchHit:
    name = asset.article.name if asset.article else asset.model or "Device"
    location = asset.location.name if asset.location else ""
    place = f" {asset.installation_place}" if asset.installation_place else ""
    return SearchHit(
        kind="asset",
        id=asset.id,
        title=f"{asset.asset_tag or asset.serial_number} · {name}",
        subtitle=f"{label(asset.status, AssetStatus)} · {location}{place} · {asset.serial_number}",
        status=asset.status,
        tone=tone(asset.status, AssetStatus),
        href=f"/stock/assets?asset={asset.id}",
        exact=exact,
    )


def contact_hit(contact: Contact, exact: bool) -> SearchHit:
    subtitle = contact.user_id + (f" · {contact.email}" if contact.email else "")
    return SearchHit(
        kind="contact",
        id=contact.id,
        title=contact.display_name,
        subtitle=subtitle,
        status=None,
        tone=None,
        href=f"/service-requests?contactId={contact.id}&includeClosed=true",
        exact=exact,
    )


def place_hit(place: str, count: int) -> SearchHit:
    return SearchHit(
        kind="installation_place",
        id=None,
        title=place,
        subtitle=f"{count} asset{'s' if count != 1 else ''}",
        status=None,
        tone=None,
        href=f"/stock/assets?installationPlace={place}",
        exact=True,
    )


# --- core provider ------------------------------------------------------------------------------


async def _contact_records(session: AsyncSession, contact: Contact, exact: bool) -> list[SearchHit]:
    srs = (
        await session.scalars(
            select(ServiceRequest)
            .where(ServiceRequest.contact_id == contact.id)
            .order_by(ServiceRequest.imported_at.desc())
            .limit(PER_GROUP)
        )
    ).all()
    leases = (
        await session.scalars(
            select(LeaseReturn).where(LeaseReturn.contact_id == contact.id).limit(PER_GROUP)
        )
    ).all()
    return [
        contact_hit(contact, exact),
        *(sr_hit(s, False) for s in srs),
        *(lease_hit(item_, False) for item_ in leases),
    ]


async def exact_hits(session: AsyncSession, q: str) -> list[SearchHit]:
    code = q.upper()
    if _SR.match(code):
        sr = (
            await session.scalars(select(ServiceRequest).where(ServiceRequest.number == code))
        ).first()
        return [sr_hit(sr, True)] if sr else []
    if _TASK.match(code):
        sr = (
            await session.scalars(select(ServiceRequest).where(ServiceRequest.task_number == code))
        ).first()
        return [sr_hit(sr, True)] if sr else []
    if _TAG.match(code):
        hits: list[SearchHit] = []
        asset = (await session.scalars(select(Asset).where(Asset.asset_tag == code))).first()
        if asset:
            hits.append(asset_hit(asset, True))
        lease = (
            await session.scalars(select(LeaseReturn).where(LeaseReturn.asset_tag == code))
        ).first()
        if lease:
            hits.append(lease_hit(lease, not hits))
        if hits:
            return hits
    place = normalize_place(code)
    if place:
        assets = (
            await session.scalars(
                select(Asset)
                .where(Asset.installation_place == place)
                .order_by(Asset.id)
                .limit(PER_GROUP)
            )
        ).all()
        if assets:
            return [place_hit(place, len(assets)), *(asset_hit(a, False) for a in assets)]
    if _USER_ID.match(code):
        contact = (await session.scalars(select(Contact).where(Contact.user_id == code))).first()
        if contact:
            return await _contact_records(session, contact, True)
    asset = (await session.scalars(select(Asset).where(Asset.serial_number == code))).first()
    return [asset_hit(asset, True)] if asset else []


async def fuzzy_hits(session: AsyncSession, q: str) -> list[SearchHit]:
    term = q.strip()
    hits: list[SearchHit] = []
    sr_text = ServiceRequest.number + " " + func.coalesce(ServiceRequest.task_number, "")
    srs = (
        await session.scalars(
            select(ServiceRequest)
            .where(
                or_(func.similarity(sr_text, term) > SIMILARITY, sr_text.ilike(f"%{term.upper()}%"))
            )
            .order_by(func.similarity(sr_text, term).desc(), ServiceRequest.id)
            .limit(PER_GROUP)
        )
    ).all()
    hits += [sr_hit(s, False) for s in srs]

    name_text = (
        func.coalesce(Contact.first_name, "")
        + " "
        + Contact.last_name
        + " "
        + func.coalesce(Contact.email, "")
    )
    contacts = (
        await session.scalars(
            select(Contact)
            .where(or_(func.similarity(name_text, term) > SIMILARITY, name_text.ilike(f"%{term}%")))
            .order_by(func.similarity(name_text, term).desc(), Contact.id)
            .limit(PER_GROUP)
        )
    ).all()
    for contact in contacts:
        hits += await _contact_records(session, contact, False)

    asset_text = Asset.serial_number + " " + func.coalesce(Asset.asset_tag, "")
    assets = (
        await session.scalars(
            select(Asset)
            .where(
                or_(
                    func.similarity(asset_text, term) > SIMILARITY,
                    asset_text.ilike(f"%{term.upper()}%"),
                )
            )
            .order_by(func.similarity(asset_text, term).desc(), Asset.id)
            .limit(PER_GROUP)
        )
    ).all()
    hits += [asset_hit(a, False) for a in assets]

    leases = (
        await session.scalars(
            select(LeaseReturn)
            .where(cast(LeaseReturn.asset_tag, String).ilike(f"%{term.upper()}%"))
            .order_by(LeaseReturn.asset_tag)
            .limit(PER_GROUP)
        )
    ).all()
    hits += [lease_hit(item_, False) for item_ in leases]
    return hits


async def core_search_provider(session: AsyncSession, q: str, user: User) -> list[SearchHit]:
    return [*(await exact_hits(session, q)), *(await fuzzy_hits(session, q))]


# --- aggregation --------------------------------------------------------------------------------

_providers: list[SearchProvider] = [core_search_provider]


def register_search_provider(provider: SearchProvider) -> None:
    if provider not in _providers:
        _providers.append(provider)


async def search(session: AsyncSession, q: str, user: User, limit: int = TOTAL) -> SearchResponse:
    query = q.strip()
    collected: list[SearchHit] = []
    for provider in _providers:
        collected += await provider(session, query, user)
    collected = [h for h in collected if can(user, KIND_PERMISSIONS.get(h.kind, "sr.read"))]
    seen: set[tuple[str, int | None, str]] = set()
    unique: list[SearchHit] = []
    for hit in sorted(collected, key=lambda h: not h.exact):  # stable: exact first
        key = (hit.kind, hit.id, hit.title)
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    groups: list[SearchGroup] = []
    total = 0
    for kind in KIND_ORDER:
        hits = [h for h in unique if h.kind == kind][:PER_GROUP]
        hits = hits[: max(0, min(limit, TOTAL) - total)]
        if hits:
            groups.append(SearchGroup(kind=kind, label=KIND_LABELS[kind], hits=hits))
            total += len(hits)
    exact = next((h for h in unique if h.exact and h.kind != "installation_place"), None) or next(
        (h for h in unique if h.exact), None
    )
    return SearchResponse(query=query, exact=exact, groups=groups)
