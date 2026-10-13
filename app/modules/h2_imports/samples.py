"""Sample import files (SPEC §7.9), generated with the fake ``TicketingImportSource`` (seed 7)
and the seeded data, so updates, lease linking, the hold group, Campus and VIP all show.

Usage: ``uv run python -m app.modules.h2_imports.samples [--out samples]`` against a seeded
database writes ``samples/*.csv`` (committed). ``GET /imports/templates/{type}/sample``
builds the same file on the fly (dates relative to today).
"""

from __future__ import annotations

import argparse
import csv
import io
import random
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gateways
from app.core.config import get_settings
from app.core.enums import (
    OLD_SLOTS,
    AssetStatus,
    ImportType,
    LeaseReturnStatus,
    ServiceRequestStatus,
)
from app.core.models import (
    Asset,
    Contact,
    LeaseReturn,
    ServiceRequest,
    ServiceRequestItem,
    VipEntry,
)

from .importing.formats import OLD_DEVICE_STOCKROOM
from .importing.packed import build_packed

SEED = 7
SAMPLES_DIR = Path(__file__).resolve().parents[3] / "samples"

#: file name (without .csv) → import type (``None`` = deactivation file for H5)
SAMPLE_FILES: dict[str, ImportType | None] = {
    "lease_returns": ImportType.LEASE_RETURNS,
    "service_requests": ImportType.SERVICE_REQUESTS,
    "service_requests_with_errors": ImportType.SERVICE_REQUESTS,
    "contacts": ImportType.CONTACTS,
    "assets_new": ImportType.ASSETS_NEW,
    "asset_status_sync": ImportType.ASSET_STATUS_SYNC,
    "deactivation": None,
}
#: the sample offered for each type (``GET /imports/templates/{type}/sample``)
DEFAULT_SAMPLE: dict[ImportType, str] = {
    ImportType.LEASE_RETURNS: "lease_returns",
    ImportType.SERVICE_REQUESTS: "service_requests",
    ImportType.CONTACTS: "contacts",
    ImportType.ASSETS_NEW: "assets_new",
    ImportType.ASSET_STATUS_SYNC: "asset_status_sync",
}
_EARLY_SR = (
    ServiceRequestStatus.INVITED,
    ServiceRequestStatus.REMINDER_1_SENT,
    ServiceRequestStatus.REMINDER_2_SENT,
)
_OPEN_LEASE = (
    LeaseReturnStatus.IMPORTED,
    LeaseReturnStatus.INFO_SENT,
    LeaseReturnStatus.REMINDER_1_SENT,
)


def _read(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    return list(reader.fieldnames or []), list(reader)


def _write(columns: list[str], rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") or "" for c in columns})
    return buffer.getvalue().encode("utf-8")


def _pairs(packed: str) -> list[tuple[str, str]]:
    """``[A: 1],[B: 2]`` → ``[("A", "1"), ("B", "2")]`` (generated cells are well-formed)."""
    text = packed.strip()
    if not text:
        return []
    pairs = []
    for part in text[1:-1].split("],["):
        key, _, value = part.partition(":")
        pairs.append((key.strip(), value.strip()))
    return pairs


def _set_pair(pairs: list[tuple[str, str]], key: str, value: str) -> list[tuple[str, str]]:
    if any(k == key for k, _ in pairs):
        return [(k, value if k == key else v) for k, v in pairs]
    return [*pairs, (key, value)]


def _person(row: dict[str, str], contact: Contact | None, user_id: str) -> None:
    row["user_id"] = user_id
    if contact is not None:
        row["first_name"] = contact.first_name or ""
        row["last_name"] = contact.last_name
        row["email"] = contact.email or ""
        row["gender"] = str(contact.gender)
        if row.get("short_description"):
            kind = row["short_description"].split(" for ", 1)[0]
            row["short_description"] = f"{kind} for {contact.display_name}"


async def _consistent_people(session: AsyncSession, rows: list[dict[str, str]]) -> None:
    """One person per user id: known contacts keep their master data, repeated new ids reuse
    the first row's person (the fake export draws names independently per row)."""
    ids = {row["user_id"] for row in rows if row.get("user_id")}
    known = {
        c.user_id: c
        for c in (await session.scalars(select(Contact).where(Contact.user_id.in_(ids)))).all()
    }
    first: dict[str, dict[str, str]] = {}
    for n, row in enumerate(rows):
        user_id = row.get("user_id")
        if not user_id:
            continue
        if user_id in known and known[user_id].email is None:
            # known contact without email: give the row its own (new) requester instead
            user_id = row["user_id"] = f"E{109100 + n:06d}"
        if user_id in known:
            _person(row, known[user_id], user_id)
        elif user_id in first:
            for key in ("gender", "first_name", "last_name", "email"):
                row[key] = first[user_id].get(key, "")
            if row.get("short_description"):
                kind = row["short_description"].split(" for ", 1)[0]
                name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
                row["short_description"] = f"{kind} for {name}"
        else:
            first[user_id] = row


async def _contact(session: AsyncSession, user_id: str) -> Contact | None:
    return (await session.scalars(select(Contact).where(Contact.user_id == user_id))).first()


# --- per file ------------------------------------------------------------------------------


async def _lease_returns(session: AsyncSession) -> bytes:
    data = gateways.ticketing().generate_export(
        ImportType.LEASE_RETURNS, rows=35, seed=SEED, start_number=150001
    )
    columns, rows = _read(data)
    await _consistent_people(session, rows)
    existing = (
        await session.scalars(
            select(LeaseReturn)
            .where(LeaseReturn.status.in_(_OPEN_LEASE), LeaseReturn.is_vip.is_(False))
            .order_by(LeaseReturn.retirement_date, LeaseReturn.id)
            .limit(5)
        )
    ).all()
    for n, lease in enumerate(existing):
        contact = lease.contact
        row = {
            "asset_tag": lease.asset_tag,
            "serial_number": lease.serial_number or "",
            "model_category": lease.model_category or "Notebook",
            "checked_in_date": lease.checked_in_at.date().isoformat()
            if lease.checked_in_at
            else "",
            "retirement_date": lease.retirement_date.isoformat(),
            "model": lease.model or "",
            "install_status": "In use",
            "ci_name": f"NB-{70100 + n}",  # changed → the row counts as an update
            "cost_center": lease.cost_center or "",
            "escalation_email": "",
        }
        _person(row, contact, lease.user_id)
        rows.append(row)
    return _write(columns, rows)


async def _linkable_leases(session: AsyncSession, limit: int) -> list[LeaseReturn]:
    return list(
        (
            await session.scalars(
                select(LeaseReturn)
                .where(
                    LeaseReturn.status.in_(_OPEN_LEASE),
                    LeaseReturn.service_request_id.is_(None),
                    LeaseReturn.is_vip.is_(False),
                )
                .order_by(LeaseReturn.retirement_date.desc(), LeaseReturn.id)
                .limit(limit)
            )
        ).all()
    )


async def _service_requests(session: AsyncSession) -> bytes:
    data = gateways.ticketing().generate_export(
        ImportType.SERVICE_REQUESTS, rows=30, seed=SEED, start_number=12301
    )
    columns, rows = _read(data)
    await _consistent_people(session, rows)
    # 3 rows name seeded lease returns as old devices → linked on import (SPEC §6.4)
    for row, lease in zip(rows[0:3], await _linkable_leases(session, 3), strict=False):
        pairs = _pairs(row["order_details"])
        pairs = _set_pair(pairs, "Procurement type", "Replacement")
        pairs = _set_pair(pairs, "Old base device", lease.asset_tag)
        if not any(k == "New base device type" for k, _ in pairs):
            pairs = _set_pair(pairs, "New base device type", "Laptop Standard 14")
        row["order_details"] = build_packed(pairs)
        row["catalog_item"] = "Workplace hardware replacement"
        _person(row, lease.contact, lease.user_id)
    # 2 rows in the ITSM hold group → hardware unavailable (SPEC §6.5)
    for row in rows[3:5]:
        row["assignment_group"] = get_settings().itsm_hold_assignment_group
    # 1 Campus address (zip rule 10500 → Riverton campus)
    campus = rows[5]
    address = _pairs(campus["delivery_address"])
    address = _set_pair(address, "Street", "1 Campus Way")
    address = _set_pair(address, "Zip", "10500")
    address = _set_pair(address, "City", "Riverton")
    campus["delivery_address"] = build_packed(address)
    # 1 VIP requester → imported_vip
    vip = (await session.scalars(select(VipEntry).order_by(VipEntry.id).limit(1))).first()
    if vip is not None:
        row = rows[6]
        contact = await _contact(session, vip.user_id)
        _person(row, contact, vip.user_id)
        if contact is None:
            row["first_name"] = vip.first_name or ""
            row["last_name"] = vip.last_name
    return _write(columns, rows)


async def _service_requests_with_errors(session: AsyncSession) -> bytes:
    data = gateways.ticketing().generate_export(
        ImportType.SERVICE_REQUESTS, rows=25, seed=SEED + 1, start_number=12401, with_errors=True
    )
    columns, rows = _read(data)
    # the email_missing warning needs a requester that is not a known contact yet
    rows[0]["user_id"] = "E109001"
    rows[0]["email"] = ""
    await _consistent_people(session, rows)
    return _write(columns, rows)


async def _contacts(session: AsyncSession) -> bytes:
    return gateways.ticketing().generate_export(
        ImportType.CONTACTS, rows=30, seed=SEED, start_number=100501
    )


async def _assets_new(session: AsyncSession) -> bytes:
    data = gateways.ticketing().generate_export(
        ImportType.ASSETS_NEW, rows=60, seed=SEED, start_number=260001
    )
    columns, rows = _read(data)
    rows[10]["model"] = "KD-AERO15-X9"  # unknown_model warning
    rows[31]["model"] = "LM-VIEW-49U"  # unknown_model warning
    return _write(columns, rows)


async def _asset_status_sync(session: AsyncSession) -> bytes:
    data = gateways.ticketing().generate_export(
        ImportType.ASSET_STATUS_SYNC, rows=20, seed=SEED, start_number=170001
    )
    columns, rows = _read(data)
    # old devices of open requests: 4 confirmed at their users, 2 returned to the hub
    olds = (
        await session.execute(
            select(Asset, ServiceRequest)
            .join(ServiceRequestItem, ServiceRequestItem.asset_id == Asset.id)
            .join(ServiceRequest, ServiceRequest.id == ServiceRequestItem.service_request_id)
            .where(
                Asset.status == AssetStatus.IN_USE,
                ServiceRequestItem.slot.in_(OLD_SLOTS),
                ServiceRequest.status.in_(_EARLY_SR),
            )
            .order_by(Asset.id)
            .limit(6)
        )
    ).all()
    for n, (row, (asset, sr)) in enumerate(zip(rows[0:6], olds, strict=False)):
        row["serial_number"] = asset.serial_number
        row["asset_tag"] = asset.asset_tag or ""
        row["model"] = asset.model or row["model"]
        if n < 4:
            row["install_state"], row["stockroom"] = "In use", ""
            row["assigned_user_id"] = sr.contact.user_id
            row["sr_number"] = ""
        else:
            row["install_state"], row["stockroom"] = "In stock", OLD_DEVICE_STOCKROOM
            row["assigned_user_id"] = ""
            row["sr_number"] = sr.number
    return _write(columns, rows)


async def _deactivation(session: AsyncSession) -> bytes:
    """For H5's deactivation upload: 20 collected, 3 unknown, 2 assigned (rejected)."""
    collected = (
        await session.scalars(
            select(Asset).where(Asset.status == AssetStatus.COLLECTED).order_by(Asset.id).limit(20)
        )
    ).all()
    assigned = (
        await session.scalars(
            select(Asset).where(Asset.status == AssetStatus.ASSIGNED).order_by(Asset.id).limit(2)
        )
    ).all()
    rng = random.Random(SEED)  # noqa: S311 - demo data
    unknown = [
        "KDZ" + "".join(rng.choice("0123456789BCDFGHJKLMNPQRSTVWXYZ") for _ in range(7))
        for _ in range(3)
    ]
    rows = [{"serial_number": a.serial_number, "asset_tag": a.asset_tag or ""} for a in collected]
    rows += [{"serial_number": s, "asset_tag": ""} for s in unknown]
    rows += [{"serial_number": a.serial_number, "asset_tag": a.asset_tag or ""} for a in assigned]
    return _write(["serial_number", "asset_tag"], rows)


_BUILDERS = {
    "lease_returns": _lease_returns,
    "service_requests": _service_requests,
    "service_requests_with_errors": _service_requests_with_errors,
    "contacts": _contacts,
    "assets_new": _assets_new,
    "asset_status_sync": _asset_status_sync,
    "deactivation": _deactivation,
}


async def build_sample(session: AsyncSession, name: str) -> bytes:
    """CSV bytes of sample ``name`` (a key of :data:`SAMPLE_FILES`)."""
    return await _BUILDERS[name](session)


async def write_samples(session: AsyncSession, out: Path = SAMPLES_DIR) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - developer tool
    written = []
    for name in SAMPLE_FILES:
        path = out / f"{name}.csv"
        path.write_bytes(await build_sample(session, name))
        written.append(path)
    return written


def _main() -> None:  # pragma: no cover - developer tool
    from app.core.aio import run
    from app.core.db import open_session
    from app.core.slices import load_slices

    parser = argparse.ArgumentParser(description="Write the H2 sample import files.")
    parser.add_argument("--out", type=Path, default=SAMPLES_DIR)
    args = parser.parse_args()
    load_slices()

    async def go() -> None:
        async with open_session() as session:
            for path in await write_samples(session, args.out):
                sys.stdout.write(f"wrote {path}" + chr(10))

    run(go())


if __name__ == "__main__":  # pragma: no cover
    _main()
