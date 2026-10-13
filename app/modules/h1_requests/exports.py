"""xlsx exports (SPEC §6.20): frozen header, auto-filter, column widths, kept 24 h."""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

import xlsxwriter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.enums import (
    AttentionFlag,
    DeliveryType,
    LeaseReturnStatus,
    PipelineStage,
    ServiceRequestStatus,
    label,
    stage_of,
)
from app.core.files import signed_url, store_file
from app.core.models import LeaseReturn, ServiceRequest, User
from app.core.read_models import sr_attention
from app.core.schemas import DownloadLink
from app.core.spreadsheets import XLSX_WORKBOOK_OPTIONS
from app.core.timeutil import business_today

EXPORT_TTL = timedelta(hours=24)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SR_COLUMNS: tuple[tuple[str, int], ...] = (
    ("SR number", 12), ("Task number", 14), ("Status", 22), ("Stage", 16), ("User id", 10),
    ("User name", 24), ("Email", 32), ("Region", 14), ("Hub", 8), ("Delivery type", 22),
    ("Campus", 8), ("VIP", 6), ("Appointment", 18), ("Communication date", 14),
    ("Imported at", 18), ("ITSM state", 16), ("Cost centre", 12), ("Attention", 40),
)  # fmt: skip
LEASE_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Asset tag", 12), ("Serial", 16), ("Model", 24), ("Retirement date", 14), ("User id", 10),
    ("User name", 24), ("Email", 32), ("Status", 20), ("Communication date", 14),
    ("Linked SR", 12), ("VIP", 6), ("Campus", 8),
)  # fmt: skip


def _local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(get_settings().tz).replace(tzinfo=None)


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def workbook(sheet: str, columns: Sequence[tuple[str, int]], rows: list[list[Any]]) -> bytes:
    buffer = io.BytesIO()
    book = xlsxwriter.Workbook(
        buffer, {**XLSX_WORKBOOK_OPTIONS, "in_memory": True, "remove_timezone": True}
    )
    ws = book.add_worksheet(sheet)
    header = book.add_format({"bold": True, "bg_color": "#E8EBEF", "border": 1})
    date_fmt = book.add_format({"num_format": "yyyy-mm-dd"})
    datetime_fmt = book.add_format({"num_format": "yyyy-mm-dd hh:mm"})
    for col, (title, width) in enumerate(columns):
        ws.write(0, col, title, header)
        ws.set_column(col, col, width)
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row):
            if isinstance(value, datetime):
                ws.write_datetime(r, c, value, datetime_fmt)
            elif isinstance(value, date):
                ws.write_datetime(r, c, datetime(value.year, value.month, value.day), date_fmt)  # noqa: DTZ001
            elif value is None:
                ws.write_blank(r, c, None)
            elif isinstance(value, str):
                ws.write_string(r, c, value)  # imported text is never a formula
            else:
                ws.write(r, c, value)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, max(len(rows), 1), len(columns) - 1)
    book.close()
    return buffer.getvalue()


def sr_rows(srs: Sequence[ServiceRequest]) -> list[list[Any]]:
    today = business_today()
    rows: list[list[Any]] = []
    for sr in srs:
        contact = sr.contact
        delivery = sr.effective_delivery_type
        rows.append(
            [
                sr.number,
                sr.task_number,
                label(ServiceRequestStatus(sr.status)),
                label(PipelineStage(stage_of(sr.status))),
                contact.user_id if contact else None,
                contact.display_name if contact else None,
                contact.email if contact else None,
                sr.region.name if sr.region else None,
                sr.supplying_site.code if sr.supplying_site else None,
                label(DeliveryType(delivery)) if delivery else None,
                _yes(sr.is_campus),
                _yes(sr.is_vip),
                _local(sr.appointment_start_at),
                sr.communication_date,
                _local(sr.imported_at),
                sr.itsm_state,
                sr.cost_center,
                ", ".join(label(AttentionFlag(f)) for f in sr_attention(sr, today=today)),
            ]
        )
    return rows


def lease_rows(leases: Sequence[LeaseReturn]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for lease in leases:
        contact = lease.contact
        rows.append(
            [
                lease.asset_tag,
                lease.serial_number,
                lease.model,
                lease.retirement_date,
                lease.user_id,
                contact.display_name if contact else None,
                contact.email if contact else None,
                label(LeaseReturnStatus(lease.status)),
                lease.communication_date,
                lease.service_request.number if lease.service_request else None,
                _yes(lease.is_vip),
                _yes(lease.is_campus),
            ]
        )
    return rows


async def store_export(
    session: AsyncSession, data: bytes, filename: str, user: User
) -> DownloadLink:
    stored = await store_file(
        session, data, filename, XLSX, "export", ttl=EXPORT_TTL, created_by=user
    )
    return signed_url(stored)


def export_filename(prefix: str) -> str:
    return f"{prefix}-{business_today().isoformat()}.xlsx"
