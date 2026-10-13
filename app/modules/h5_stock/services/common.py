"""Shared helpers: CSV files (SPEC §6.20), temp exports, debounced stock recalculation."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import after_commit
from app.core.enums import SiteKind
from app.core.errors import ValidationProblem
from app.core.files import signed_url, store_file
from app.core.models import Site, User
from app.core.schemas import DownloadLink, EntityRef
from app.core.timeutil import business_today

log = structlog.get_logger(__name__)

CSV_TYPE = "text/csv"
EXPORT_TTL = timedelta(hours=24)
RECALC_TASK = "stock.recalculate"
RECALC_LOCK = "stock.recalculate"
#: queueing lock of the debounced (60 s) recalculation deferred after imports/deactivation/
#: inventory close — at most one pending debounced run (SPEC §6.7).
RECALC_DEBOUNCE_LOCK = "stock.recalculate.debounce"
RECALC_DEBOUNCE_SECONDS = 60


def csv_bytes(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    """UTF-8 **with BOM**, comma, RFC 4180 quoting, CRLF (SPEC §6.20)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if value is None else _cell(value) for value in row])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def _cell(value: Any) -> str:
    if isinstance(value, datetime):
        from app.core.notifications.renderer import format_datetime

        return format_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


async def store_export(
    session: AsyncSession,
    data: bytes,
    filename: str,
    *,
    content_type: str = CSV_TYPE,
    purpose: str = "export",
    entity: EntityRef | tuple[str, int] | None = None,
    actor: User | None = None,
    ttl: timedelta | None = EXPORT_TTL,
) -> DownloadLink:
    stored = await store_file(
        session,
        data,
        filename,
        content_type,
        purpose,
        entity=entity,
        ttl=ttl,
        created_by=actor,
    )
    return signed_url(stored)


def stamp(day: date | None = None) -> str:
    return (day or business_today()).strftime("%Y%m%d")


async def hubs(session: AsyncSession) -> list[Site]:
    stmt = (
        select(Site)
        .where(Site.kind == SiteKind.HUB, Site.is_active)
        .order_by(Site.sort_order, Site.id)
    )
    return list((await session.scalars(stmt)).all())


async def require_hub(session: AsyncSession, site_id: int, field: str = "siteId") -> Site:
    site = await session.get(Site, site_id)
    if site is None or site.kind != SiteKind.HUB:
        raise ValidationProblem({field: ["Choose a hub (Riverton, Hillford or Eastgate)."]})
    return site


async def selected_hubs(
    session: AsyncSession, site_ids: Sequence[int | str] | None, field: str = "siteId"
) -> list[Site]:
    """Hubs filtered by ``?siteId=`` (default: all active hubs); unknown ids → 422."""
    all_hubs = await hubs(session)
    if not site_ids:
        return all_hubs
    wanted = [int(i) for i in site_ids if isinstance(i, int)]
    by_id = {s.id: s for s in all_hubs}
    unknown = [i for i in wanted if i not in by_id]
    if unknown:
        raise ValidationProblem({field: [f"Site {i} is not a hub." for i in unknown]})
    return [s for s in all_hubs if s.id in set(wanted)]


def schedule_recalculation(session: AsyncSession) -> None:
    """Defer a debounced ``stock.recalculate`` after the current unit of work commits."""
    after_commit(session, defer_debounced_recalculation)


async def defer_debounced_recalculation() -> None:
    from procrastinate.exceptions import AlreadyEnqueued

    from app.jobs.app import job_app

    task = job_app.configure_task(
        RECALC_TASK,
        queueing_lock=RECALC_DEBOUNCE_LOCK,
        schedule_in={"seconds": RECALC_DEBOUNCE_SECONDS},
    )
    try:
        await task.defer_async()
    except AlreadyEnqueued:
        log.debug("stock_recalculate_already_pending")


def team_mailbox() -> str:
    return get_settings().mail_team_mailbox
