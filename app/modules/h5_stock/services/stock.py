"""Stock matrix, buffers, export and the recalculation run (SPEC §6.7/§6.8, API.md §8.1)."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    DEMAND_STATUSES,
    ArticleCategory,
    JobKind,
    JobStatus,
    ServiceRequestStatus,
)
from app.core.errors import ValidationProblem
from app.core.events import StockRecalculated, emit
from app.core.models import (
    Article,
    ArticleSiteSetting,
    BackgroundJob,
    ServiceRequest,
    ServiceRequestItem,
    Site,
    User,
)
from app.core.pagination import escape_like
from app.core.read_models.refs import site_ref
from app.core.schemas import DownloadLink
from app.core.stock_math import UNMATCHED_DEMAND_SQL, above_max, below_min, order_qty, recalculate
from app.core.timeutil import now_utc

from ..schemas import (
    BuffersPatch,
    StockArticleRef,
    StockCell,
    StockCellWithArticle,
    StockMatrix,
    StockMatrixMeta,
    StockRow,
    StockRowTotals,
    UnmatchedDemand,
)
from .common import csv_bytes, require_hub, selected_hubs, stamp, store_export

#: A queued/running recalculation older than this is considered stale (no worker running).
RECALC_STALE_AFTER = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class MatrixFilters:
    site_ids: Sequence[int | str] | None = None
    categories: Sequence[ArticleCategory] | None = None
    q: str | None = None
    only_shortfall: bool = False
    include_inactive: bool = False


def build_cell(setting: ArticleSiteSetting) -> StockCell:
    stock = setting.stock_qty
    return StockCell(
        site_id=setting.site_id,
        goods_in=setting.stock_goods_in,
        staging=setting.stock_staging,
        goods_out=setting.stock_goods_out,
        stock=stock,
        demand=setting.demand_qty,
        buffer=setting.buffer_qty,
        order_qty=order_qty(setting.demand_qty, stock, setting.buffer_qty),
        min_qty=setting.min_qty,
        max_qty=setting.max_qty,
        below_min=below_min(stock, 0, setting.min_qty),
        above_max=above_max(stock, 0, setting.max_qty),
        last_order_date=setting.last_order_date,
        last_ordered_qty=setting.last_ordered_qty,
    )


def stock_article_ref(article: Article) -> StockArticleRef:
    return StockArticleRef(
        id=article.id,
        number=article.number,
        name=article.name,
        model_number=article.model_number,
        internal_description=article.internal_description,
        category=article.category,
        is_active=article.is_active,
    )


def _shortfall(cell: StockCell) -> bool:
    return cell.order_qty > 0 or cell.demand > cell.stock


async def matrix_rows(
    session: AsyncSession, filters: MatrixFilters
) -> tuple[list[Site], list[tuple[Article, list[ArticleSiteSetting]]]]:
    sites = await selected_hubs(session, filters.site_ids)
    site_ids = [s.id for s in sites]
    stmt = (
        select(ArticleSiteSetting)
        .join(Article, Article.id == ArticleSiteSetting.article_id)
        .where(ArticleSiteSetting.site_id.in_(site_ids))
        .order_by(Article.number, ArticleSiteSetting.site_id)
    )
    if not filters.include_inactive:
        stmt = stmt.where(Article.is_active)
    if filters.categories:
        stmt = stmt.where(Article.category.in_(filters.categories))
    if filters.q and filters.q.strip():
        pattern = f"%{escape_like(filters.q.strip())}%"
        stmt = stmt.where(
            or_(
                Article.number.ilike(pattern, escape="\\"),
                Article.name.ilike(pattern, escape="\\"),
                Article.internal_description.ilike(pattern, escape="\\"),
                Article.model_number.ilike(pattern, escape="\\"),
            )
        )
    settings = (await session.scalars(stmt)).all()
    order = {site.id: index for index, site in enumerate(sites)}
    grouped: dict[int, tuple[Article, list[ArticleSiteSetting]]] = {}
    for setting in settings:
        entry = grouped.setdefault(setting.article_id, (setting.article, []))
        entry[1].append(setting)
    rows = []
    for article, cells in grouped.values():
        cells.sort(key=lambda s: order[s.site_id])
        rows.append((article, cells))
    return sites, rows


async def is_recalculating(session: AsyncSession) -> bool:
    stmt = select(func.count()).where(
        BackgroundJob.kind == JobKind.STOCK_RECALCULATE,
        BackgroundJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
        BackgroundJob.created_at > now_utc() - RECALC_STALE_AFTER,
    )
    return bool(await session.scalar(stmt))


async def unmatched_demand(session: AsyncSession, site_ids: Sequence[int]) -> list[UnmatchedDemand]:
    rows = (await session.execute(text(UNMATCHED_DEMAND_SQL))).mappings().all()
    return [
        UnmatchedDemand(
            requested_type=row["requested_type"] or "—",
            site_id=row["site_id"],
            count=int(row["count"]),
        )
        for row in rows
        if row["site_id"] is None or row["site_id"] in site_ids
    ]


def demand_status_clause() -> Any:
    return or_(
        ServiceRequest.status.in_(DEMAND_STATUSES),
        (ServiceRequest.status == ServiceRequestStatus.RESCHEDULE_NEEDED)
        & ServiceRequest.resume_status.is_distinct_from(ServiceRequestStatus.SHIPPED),
    )


async def unresolved_demand(session: AsyncSession) -> int:
    """New items of demand-relevant SRs without a supplying hub (no region resolved)."""
    stmt = (
        select(func.count())
        .select_from(ServiceRequestItem)
        .join(ServiceRequest, ServiceRequest.id == ServiceRequestItem.service_request_id)
        .where(
            func.left(ServiceRequestItem.slot, 4) == "new_",
            ServiceRequest.supplying_site_id.is_(None),
            demand_status_clause(),
        )
    )
    return int(await session.scalar(stmt) or 0)


async def build_matrix(session: AsyncSession, filters: MatrixFilters) -> StockMatrix:
    sites, grouped = await matrix_rows(session, filters)
    rows: list[StockRow] = []
    calculated: list[datetime] = []
    for article, settings in grouped:
        cells = [build_cell(s) for s in settings]
        calculated.extend(s.calculated_at for s in settings if s.calculated_at is not None)
        if filters.only_shortfall and not any(_shortfall(c) for c in cells):
            continue
        rows.append(
            StockRow(
                article=stock_article_ref(article),
                cells=cells,
                totals=StockRowTotals(
                    stock=sum(c.stock for c in cells),
                    demand=sum(c.demand for c in cells),
                    order_qty=sum(c.order_qty for c in cells),
                ),
            )
        )
    site_ids = [s.id for s in sites]
    if not calculated:
        latest = await session.scalar(select(func.max(ArticleSiteSetting.calculated_at)))
        calculated_at = latest
    else:
        calculated_at = max(calculated)
    return StockMatrix(
        calculated_at=calculated_at,
        sites=[r for s in sites if (r := site_ref(s)) is not None],
        rows=rows,
        meta=StockMatrixMeta(
            unmatched_demand=await unmatched_demand(session, site_ids),
            unresolved_demand=await unresolved_demand(session),
            recalculating=await is_recalculating(session),
        ),
    )


async def update_buffers(
    session: AsyncSession, body: BuffersPatch, actor: User
) -> list[StockCellWithArticle]:
    site = await require_hub(session, body.site_id)
    article_ids = [entry.article_id for entry in body.buffers]
    settings = {
        s.article_id: s
        for s in (
            await session.scalars(
                select(ArticleSiteSetting).where(
                    ArticleSiteSetting.site_id == site.id,
                    ArticleSiteSetting.article_id.in_(article_ids),
                )
            )
        ).all()
    }
    errors: dict[str, list[str]] = {}
    for index, entry in enumerate(body.buffers):
        if entry.article_id not in settings:
            errors[f"buffers.{index}.articleId"] = ["Unknown article for this hub."]
    if errors:
        raise ValidationProblem(errors)
    result: list[StockCellWithArticle] = []
    for entry in body.buffers:
        setting = settings[entry.article_id]
        setting.buffer_qty = entry.buffer_qty
    await session.flush()
    for entry in body.buffers:
        setting = settings[entry.article_id]
        cell = build_cell(setting)
        result.append(
            StockCellWithArticle(**cell.model_dump(by_alias=False), article_id=entry.article_id)
        )
    return result


EXPORT_HEADER = (
    "Hub",
    "Article number",
    "Article",
    "Model number",
    "Device type",
    "Goods in",
    "Staging",
    "Goods out",
    "Stock",
    "Demand",
    "Buffer",
    "Order qty",
    "Min",
    "Max",
    "Last order date",
)


async def export_matrix(session: AsyncSession, filters: MatrixFilters, actor: User) -> DownloadLink:
    sites, grouped = await matrix_rows(session, filters)
    by_site = {s.id: s for s in sites}
    lines: list[list[Any]] = []
    for site in sites:
        for article, settings in grouped:
            for setting in settings:
                if setting.site_id != site.id:
                    continue
                cell = build_cell(setting)
                if filters.only_shortfall and not _shortfall(cell):
                    continue
                lines.append(
                    [
                        by_site[setting.site_id].name,
                        article.number,
                        article.name,
                        article.model_number,
                        article.internal_description,
                        cell.goods_in,
                        cell.staging,
                        cell.goods_out,
                        cell.stock,
                        cell.demand,
                        cell.buffer,
                        cell.order_qty,
                        cell.min_qty,
                        cell.max_qty,
                        cell.last_order_date,
                    ]
                )
    data = csv_bytes(EXPORT_HEADER, lines)
    return await store_export(session, data, f"stock-matrix-{stamp()}.csv", actor=actor)


async def run_recalculation(session: AsyncSession) -> dict[str, Any]:
    """Full set-based recompute (core ``stock_math``) + ``StockRecalculated`` (no commit)."""
    started = time.perf_counter()
    result = await recalculate(session, now_utc())
    await emit(session, StockRecalculated(at=result.calculated_at))
    return {
        "kind": "stock_recalculate",
        "articles": result.articles,
        "sites": result.sites,
        "calculatedAt": result.calculated_at.isoformat().replace("+00:00", "Z"),
        "durationMs": round((time.perf_counter() - started) * 1000),
    }
