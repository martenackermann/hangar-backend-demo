"""Deactivation upload (SPEC §6.10, D-12; API.md §8.4; SPEC_RESOLUTIONS #23, #24).

Synchronous: parse CSV/XLSX (``serial_number`` + optional ``asset_tag``, ≤ 10,000 rows),
de-duplicate, then per device ``deactivate`` (AssetService, the only place this internal
action runs), close the matching open lease return (system trigger), and store the
not-found / rejected CSV reports.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assets import asset_service
from app.core.config import get_settings
from app.core.enums import (
    DEACTIVATED,
    AssetAction,
    AssetStatus,
    DeactivationReason,
    LeaseReturnStatus,
    Trigger,
    label,
)
from app.core.errors import ApiError, BadRequestError
from app.core.files import Upload, signed_url, store_file
from app.core.models import Asset, LeaseReturn, StoredFile, User
from app.core.read_models.refs import user_ref
from app.core.rules import normalize_asset_tag, normalize_serial
from app.core.transitions import transition_service

from ..models import DeactivationBatch
from ..schemas import DeactivationBatchOut, DeactivationResult, DeactivationSampleRow
from .common import csv_bytes, schedule_recalculation, store_export

WORKFLOW = frozenset({AssetStatus.ASSIGNED, AssetStatus.PREPARED, AssetStatus.PROVIDED})
DEACTIVATABLE = frozenset(
    {AssetStatus.AVAILABLE, AssetStatus.IN_USE, AssetStatus.COLLECTED, AssetStatus.MISSING}
)
SAMPLE_SIZE = 50
CLOSE_REASON = "device_deactivated"
REASON_LABELS = {
    DeactivationReason.RETURNED_TO_LESSOR: "returned to lessor",
    DeactivationReason.BOUGHT_OUT: "bought out",
    DeactivationReason.DISPOSED: "disposed",
}


@dataclass(slots=True)
class ParsedRow:
    serial: str | None
    tag: str | None

    @property
    def key(self) -> str:
        return self.serial or self.tag or ""


@dataclass(slots=True)
class Outcome:
    key: str
    outcome: str
    detail: str | None
    asset: Asset | None = None


@dataclass(slots=True)
class Tally:
    deactivated: int = 0
    already: int = 0
    rejected: int = 0
    not_found: int = 0
    closed_leases: int = 0
    outcomes: list[Outcome] = field(default_factory=list)


# --- parsing --------------------------------------------------------------------------------------


def _norm_header(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _table_from_csv(data: bytes) -> tuple[list[str], list[list[str]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BadRequestError(
            "The file is not UTF-8 encoded. Save it as CSV UTF-8 and try again.",
            code="encoding_unsupported",
        ) from exc
    lines = text.splitlines()
    first = next((line for line in lines if line.strip()), "")
    delimiter = ";" if first.count(";") > first.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise BadRequestError("The file is empty.", code="empty_file")
    return rows[0], rows[1:]


def _table_from_xlsx(data: bytes) -> tuple[list[str], list[list[str]]]:
    import polars as pl

    try:
        frame = pl.read_excel(io.BytesIO(data), infer_schema_length=0)
    except Exception as exc:
        raise BadRequestError("The spreadsheet could not be read.", code="file_unreadable") from exc
    header = [str(c) for c in frame.columns]
    rows = [["" if v is None else str(v) for v in row] for row in frame.iter_rows()]
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    return header, rows


def parse_upload(upload: Upload) -> list[ParsedRow]:
    header, rows = (
        _table_from_xlsx(upload.data)
        if upload.extension == ".xlsx"
        else _table_from_csv(upload.data)
    )
    names = [_norm_header(h) for h in header]
    duplicates = sorted({n for n in names if n and names.count(n) > 1})
    if duplicates:
        raise BadRequestError(
            f"Duplicate columns: {', '.join(duplicates)}.", code="duplicate_columns"
        )
    if "serial_number" not in names:
        raise ApiError(
            "The file needs a 'serial_number' column.",
            status=422,
            code="missing_columns",
            title="Missing columns",
            errors={"file": ["Missing column: serial_number."]},
        )
    if not rows:
        raise BadRequestError("The file has no data rows.", code="empty_file")
    limit = get_settings().deactivation_max_rows
    if len(rows) > limit:
        raise ApiError(
            f"The file has {len(rows):,} rows; at most {limit:,} are allowed.",
            status=413,
            code="too_many_rows",
        )
    serial_index = names.index("serial_number")
    tag_index = names.index("asset_tag") if "asset_tag" in names else None
    parsed: list[ParsedRow] = []
    seen: set[str] = set()
    for row in rows:
        serial = normalize_serial(row[serial_index] if serial_index < len(row) else None)
        tag = None
        if tag_index is not None and tag_index < len(row):
            tag = normalize_asset_tag(row[tag_index])
        item = ParsedRow(serial, tag)
        if not item.key or item.key in seen:
            continue
        seen.add(item.key)
        parsed.append(item)
    if not parsed:
        raise BadRequestError("The file contains no serial numbers.", code="empty_file")
    return parsed


# --- applying ----------------------------------------------------------------------------------------


async def _lookup(
    session: AsyncSession, rows: list[ParsedRow]
) -> tuple[dict[str, Asset], dict[str, Asset]]:
    serials = [r.serial for r in rows if r.serial]
    tags = [r.tag for r in rows if r.tag]
    by_serial: dict[str, Asset] = {}
    by_tag: dict[str, Asset] = {}
    for chunk in _chunks(serials):
        for asset in (
            await session.scalars(select(Asset).where(Asset.serial_number.in_(chunk)))
        ).all():
            by_serial[asset.serial_number] = asset
    for chunk in _chunks(tags):
        for asset in (await session.scalars(select(Asset).where(Asset.asset_tag.in_(chunk)))).all():
            if asset.asset_tag:
                by_tag[asset.asset_tag] = asset
    return by_serial, by_tag


def _chunks(values: list[str], size: int = 1000) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


async def apply_rows(
    session: AsyncSession,
    batch: DeactivationBatch,
    rows: list[ParsedRow],
    reason: DeactivationReason,
    actor: User,
) -> Tally:
    tally = Tally()
    by_serial, by_tag = await _lookup(session, rows)
    deactivated_tags: list[str] = []
    for row in rows:
        asset = (by_serial.get(row.serial) if row.serial else None) or (
            by_tag.get(row.tag) if row.tag else None
        )
        key = asset.serial_number if asset is not None else row.key
        if asset is None:
            tally.not_found += 1
            tally.outcomes.append(Outcome(key, "not_found", "Not in the asset records."))
            continue
        status = AssetStatus(asset.status)
        if status in DEACTIVATED:
            tally.already += 1
            tally.outcomes.append(Outcome(key, "already_deactivated", label(status), asset))
            continue
        if status in WORKFLOW or status not in DEACTIVATABLE:
            sr = asset.service_request.number if asset.service_request else None
            detail = f"{label(status)} for {sr}" if sr else label(status)
            tally.rejected += 1
            tally.outcomes.append(Outcome(key, "rejected", detail, asset))
            continue
        before = f"was {label(status)} at {asset.location.name}"
        await asset_service.move(
            session,
            asset,
            AssetAction.DEACTIVATE,
            actor=actor,
            trigger=Trigger.USER,
            deactivation_reason=reason,
            source_ref=f"deactivation:{batch.id}",
        )
        tally.deactivated += 1
        tally.outcomes.append(
            Outcome(key, "deactivated", f"{REASON_LABELS[reason].capitalize()} · {before}", asset)
        )
        if asset.asset_tag:
            deactivated_tags.append(asset.asset_tag)
    tally.closed_leases = await close_leases(session, deactivated_tags, batch, actor)
    return tally


async def close_leases(
    session: AsyncSession, tags: list[str], batch: DeactivationBatch, actor: User
) -> int:
    """Close every non-closed lease return of a deactivated device (SPEC §6.4, §6.10)."""
    closed = 0
    for chunk in _chunks(tags):
        leases = (
            await session.scalars(
                select(LeaseReturn)
                .where(
                    LeaseReturn.asset_tag.in_(chunk),
                    LeaseReturn.status != LeaseReturnStatus.CLOSED,
                )
                .with_for_update()
            )
        ).all()
        for lease in leases:
            await transition_service.apply(
                session,
                lease,
                "close",
                actor=actor,
                trigger=Trigger.SYSTEM,
                payload={"closeReason": CLOSE_REASON, "deactivationBatchId": batch.id},
                comment=CLOSE_REASON,
            )
            closed += 1
    return closed


async def deactivate_upload(
    session: AsyncSession, upload: Upload, reason: DeactivationReason, actor: User
) -> DeactivationBatch:
    rows = parse_upload(upload)
    stored = await store_file(
        session,
        upload.data,
        upload.filename,
        upload.content_type,
        "deactivation_upload",
        created_by=actor,
    )
    batch = DeactivationBatch(
        reason=reason,
        original_filename=upload.filename,
        file_id=stored.id,
        total_rows=len(rows),
        created_by=actor.id,
        creator=actor,
    )
    session.add(batch)
    await session.flush()
    tally = await apply_rows(session, batch, rows, reason, actor)
    await finish_batch(session, batch, tally, actor)
    schedule_recalculation(session)
    return batch


async def finish_batch(
    session: AsyncSession, batch: DeactivationBatch, tally: Tally, actor: User | None
) -> None:
    batch.deactivated_count = tally.deactivated
    batch.already_deactivated_count = tally.already
    batch.rejected_count = tally.rejected
    batch.not_found_count = tally.not_found
    batch.closed_lease_return_count = tally.closed_leases
    batch.sample = [
        {"serialNumber": o.key, "outcome": o.outcome, "detail": o.detail}
        for o in tally.outcomes[:SAMPLE_SIZE]
    ]
    entity = ("deactivation_batch", batch.id)
    not_found = [o for o in tally.outcomes if o.outcome == "not_found"]
    if not_found:
        report = await store_file(
            session,
            csv_bytes(("serial_number",), [[o.key] for o in not_found]),
            f"deactivation-{batch.id}-not-found.csv",
            "text/csv",
            "deactivation_report",
            entity=entity,
            created_by=actor,
        )
        batch.not_found_file_id = report.id
    rejected = [o for o in tally.outcomes if o.outcome == "rejected"]
    if rejected:
        report = await store_file(
            session,
            csv_bytes(
                ("serial_number", "status", "service_request", "reason"),
                [
                    [
                        o.key,
                        o.asset.status if o.asset else "",
                        o.asset.service_request.number
                        if o.asset is not None and o.asset.service_request is not None
                        else "",
                        "Device is in a service request workflow; finish or release it first.",
                    ]
                    for o in rejected
                ],
            ),
            f"deactivation-{batch.id}-rejected.csv",
            "text/csv",
            "deactivation_report",
            entity=entity,
            created_by=actor,
        )
        batch.rejected_file_id = report.id
    await session.flush()


# --- read model ----------------------------------------------------------------------------------------


def build_batch(batch: DeactivationBatch) -> DeactivationBatchOut:
    creator = user_ref(batch.creator)
    assert creator is not None
    return DeactivationBatchOut(
        id=batch.id,
        reason=batch.reason,
        original_filename=batch.original_filename,
        total_rows=batch.total_rows,
        deactivated_count=batch.deactivated_count,
        already_deactivated_count=batch.already_deactivated_count,
        rejected_count=batch.rejected_count,
        not_found_count=batch.not_found_count,
        created_by=creator,
        created_at=batch.created_at,
    )


async def build_result(session: AsyncSession, batch: DeactivationBatch) -> DeactivationResult:
    async def link(file_id: int | None) -> Any:
        if file_id is None:
            return None
        stored = await session.get(StoredFile, file_id)
        return signed_url(stored) if stored is not None else None

    return DeactivationResult(
        **build_batch(batch).model_dump(by_alias=False),
        not_found_report=await link(batch.not_found_file_id),
        rejected_report=await link(batch.rejected_file_id),
        closed_lease_returns=batch.closed_lease_return_count,
        sample=[DeactivationSampleRow.model_validate(row) for row in batch.sample or []],
    )


async def sample_file(session: AsyncSession, actor: User) -> Any:
    """``GET /deactivations/sample`` (#23): a ready-to-upload CSV with real candidates."""
    collected = (
        await session.scalars(
            select(Asset)
            .where(Asset.status == AssetStatus.COLLECTED)
            .order_by(Asset.status_changed_at, Asset.id)
            .limit(5)
        )
    ).all()
    rows = [[a.serial_number, a.asset_tag or ""] for a in collected]
    rows.append(["KD0NOTFOUND", ""])
    data = csv_bytes(("serial_number", "asset_tag"), rows)
    return await store_export(session, data, "deactivation-sample.csv", actor=actor)
