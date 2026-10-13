"""Dashboard KPIs (API.md §8.6, SCREENS §3.2).

Reads only core tables (``service_requests``, ``status_transitions``, ``lease_returns``,
``article_site_settings``, ``background_jobs``) plus H5's own ``inventories``.
Filters: ``siteId`` → SR supplying hub / stock hub / inventory site; ``regionId`` → SR
region. Lease returns have neither, so lease tiles are global (documented decision).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.enums import (
    SR_NON_TERMINAL,
    WITH_APPT,
    InventoryStatus,
    JobKind,
    JobStatus,
    LeaseReturnStatus,
    PipelineStage,
    ServiceRequestStatus,
    stage_of,
)
from app.core.errors import ValidationProblem
from app.core.models import (
    Article,
    ArticleSiteSetting,
    BackgroundJob,
    LeaseReturn,
    Region,
    ServiceRequest,
    Site,
    StatusTransition,
)
from app.core.read_models.attention import sr_needs_attention_clause
from app.core.read_models.refs import article_ref, region_ref, site_ref
from app.core.stock_math import order_qty
from app.core.timeutil import business_datetime, business_today, now_utc, week_monday
from app.core.transitions import CHAIN_MAIL_ACTIONS

from ..models import Inventory
from ..schemas import (
    DashboardFilters,
    DashboardKpis,
    DashboardSeries,
    DashboardTiles,
    DayCount,
    RegionLoad,
    StageCount,
    StockAlert,
)

SR = ServiceRequestStatus
LR = LeaseReturnStatus
SERIES_DAYS = 14
CLOSED_WINDOW_DAYS = 14
LEASE_DUE_DAYS = 30


def _local_date(column: Any) -> Any:
    return func.date(func.timezone(get_settings().business_tz, column))


def _start(day: date) -> datetime:
    return business_datetime(day)


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int(await session.scalar(stmt) or 0)


class Kpi:
    def __init__(self, session: AsyncSession, site_id: int | None, region_id: int | None) -> None:
        self.session = session
        self.site_id = site_id
        self.region_id = region_id
        self.now = now_utc()
        self.today = business_today(self.now)
        self.monday = week_monday(self.today)

    # --- filters -------------------------------------------------------------------------------

    def sr_scope(self) -> ColumnElement[bool]:
        clauses: list[ColumnElement[bool]] = []
        if self.site_id is not None:
            clauses.append(ServiceRequest.supplying_site_id == self.site_id)
        if self.region_id is not None:
            clauses.append(ServiceRequest.region_id == self.region_id)
        return and_(true(), *clauses)

    def sr_count(self, *where: Any) -> Any:
        return select(func.count()).select_from(ServiceRequest).where(self.sr_scope(), *where)

    # --- tiles ----------------------------------------------------------------------------------

    async def tiles(self) -> DashboardTiles:
        s = self.session
        open_ = ServiceRequest.status.in_(SR_NON_TERMINAL)
        appt_date = _local_date(ServiceRequest.appointment_start_at)
        with_appt = ServiceRequest.status.in_({*WITH_APPT, SR.COMPLETED})
        week_end = self.monday + timedelta(days=6)
        last_monday = self.monday - timedelta(days=7)
        completed = ServiceRequest.status == SR.COMPLETED
        return DashboardTiles(
            open_service_requests=await _count(s, self.sr_count(open_)),
            needs_attention=await _count(
                s, self.sr_count(open_, sr_needs_attention_clause(today=self.today, now=self.now))
            ),
            appointments_today=await _count(s, self.sr_count(with_appt, appt_date == self.today)),
            appointments_this_week=await _count(
                s, self.sr_count(with_appt, appt_date >= self.monday, appt_date <= week_end)
            ),
            completed_this_week=await _count(
                s, self.sr_count(completed, ServiceRequest.completed_at >= _start(self.monday))
            ),
            completed_last_week=await _count(
                s,
                self.sr_count(
                    completed,
                    ServiceRequest.completed_at >= _start(last_monday),
                    ServiceRequest.completed_at < _start(self.monday),
                ),
            ),
            escalated_service_requests=await _count(
                s, self.sr_count(ServiceRequest.status == SR.ESCALATED)
            ),
            escalated_lease_returns=await _count(
                s, select(func.count()).where(LeaseReturn.status == LR.ESCALATED)
            ),
            hardware_unavailable=await _count(
                s, self.sr_count(ServiceRequest.status == SR.HARDWARE_UNAVAILABLE)
            ),
            reschedule_needed=await _count(
                s, self.sr_count(ServiceRequest.status == SR.RESCHEDULE_NEEDED)
            ),
            lease_returns_due_next_30_days=await _count(
                s,
                select(func.count()).where(
                    LeaseReturn.status.not_in((LR.REPLACEMENT_ORDERED, LR.CLOSED)),
                    LeaseReturn.retirement_date >= self.today,
                    LeaseReturn.retirement_date <= self.today + timedelta(days=LEASE_DUE_DAYS),
                ),
            ),
            lease_returns_overdue=await _count(
                s,
                select(func.count()).where(
                    LeaseReturn.status.not_in((LR.REPLACEMENT_ORDERED, LR.CLOSED)),
                    LeaseReturn.retirement_date < self.today,
                ),
            ),
            stock_shortfall_articles=await _count(
                s,
                select(func.count())
                .select_from(ArticleSiteSetting)
                .join(Article, Article.id == ArticleSiteSetting.article_id)
                .where(
                    Article.is_active,
                    ArticleSiteSetting.demand_qty > ArticleSiteSetting.stock_qty,
                    *self._site_filter(ArticleSiteSetting.site_id),
                ),
            ),
            open_inventories=await _count(
                s,
                select(func.count()).where(
                    Inventory.status == InventoryStatus.OPEN, *self._site_filter(Inventory.site_id)
                ),
            ),
            chain_mails_sent_today=await self.chain_mails_today(),
            avg_days_import_to_completion_30d=await self.avg_lead_time(),
            last_import_at=await self.last_import_at(),
            last_appointment_sync_at=await self.last_job(JobKind.APPOINTMENTS_SYNC),
            last_stock_calculation_at=await s.scalar(
                select(func.max(ArticleSiteSetting.calculated_at))
            ),
        )

    def _site_filter(self, column: Any) -> list[ColumnElement[bool]]:
        return [column == self.site_id] if self.site_id is not None else []

    async def chain_mails_today(self) -> int:
        conditions: list[Any] = []
        for entity_type, mail_actions in CHAIN_MAIL_ACTIONS.items():
            conditions.append(
                and_(
                    StatusTransition.entity_type == entity_type,
                    StatusTransition.action.in_(sorted(mail_actions)),
                )
            )
        from sqlalchemy import or_

        stmt = select(func.count()).where(
            or_(*conditions), StatusTransition.occurred_at >= _start(self.today)
        )
        if self.site_id is not None or self.region_id is not None:
            sr_ids = select(ServiceRequest.id).where(self.sr_scope())
            stmt = stmt.where(
                StatusTransition.entity_type == "service_request",
                StatusTransition.entity_id.in_(sr_ids),
            )
        return await _count(self.session, stmt)

    async def avg_lead_time(self) -> float | None:
        since = _start(self.today - timedelta(days=30))
        seconds = await self.session.scalar(
            select(
                func.avg(
                    func.extract("epoch", ServiceRequest.completed_at - ServiceRequest.imported_at)
                )
            ).where(
                self.sr_scope(),
                ServiceRequest.status == SR.COMPLETED,
                ServiceRequest.completed_at >= since,
            )
        )
        if seconds is None:
            return None
        return round(float(seconds) / 86400, 1)

    async def last_job(self, kind: JobKind) -> datetime | None:
        return await self.session.scalar(
            select(func.max(BackgroundJob.finished_at)).where(
                BackgroundJob.kind == kind, BackgroundJob.status == JobStatus.SUCCEEDED
            )
        )

    async def last_import_at(self) -> datetime | None:
        candidates = [
            await self.last_job(JobKind.IMPORT),
            await self.session.scalar(select(func.max(ServiceRequest.last_imported_at))),
            await self.session.scalar(select(func.max(LeaseReturn.last_seen_at))),
        ]
        values = [c for c in candidates if c is not None]
        return max(values) if values else None

    # --- breakdowns ----------------------------------------------------------------------------

    async def stage_counts(self) -> list[StageCount]:
        since = self.now - timedelta(days=CLOSED_WINDOW_DAYS)
        rows = await self.session.execute(
            select(ServiceRequest.status, func.count())
            .where(
                self.sr_scope(),
                ServiceRequest.status.in_(SR_NON_TERMINAL)
                | (ServiceRequest.status_changed_at >= since),
            )
            .group_by(ServiceRequest.status)
        )
        totals = dict.fromkeys(PipelineStage, 0)
        for status, count in rows.all():
            totals[stage_of(status)] += int(count)
        return [StageCount(stage=stage, count=count) for stage, count in totals.items()]

    async def per_day(self, column: Any, days: Sequence[date], *where: Any) -> list[DayCount]:
        local = _local_date(column)
        rows = await self.session.execute(
            select(local, func.count())
            .where(
                self.sr_scope(),
                column >= _start(days[0]),
                column < _start(days[-1] + timedelta(days=1)),
                *where,
            )
            .group_by(local)
        )
        counts = {day: int(count) for day, count in rows.all()}
        return [DayCount(date=day, count=counts.get(day, 0)) for day in days]

    async def series(self) -> DashboardSeries:
        past = [self.today - timedelta(days=n) for n in range(SERIES_DAYS - 1, -1, -1)]
        ahead = [self.today + timedelta(days=n) for n in range(SERIES_DAYS)]
        return DashboardSeries(
            completions_per_day=await self.per_day(
                ServiceRequest.completed_at, past, ServiceRequest.status == SR.COMPLETED
            ),
            appointments_per_day=await self.per_day(
                ServiceRequest.appointment_start_at,
                ahead,
                ServiceRequest.status.in_({*WITH_APPT, SR.COMPLETED}),
            ),
            imported_per_day=await self.per_day(ServiceRequest.imported_at, past),
        )

    async def region_load(self) -> list[RegionLoad]:
        open_ = ServiceRequest.status.in_(SR_NON_TERMINAL)
        booked = ServiceRequest.status.in_(WITH_APPT)
        done_week = (ServiceRequest.status == SR.COMPLETED) & (
            ServiceRequest.completed_at >= _start(self.monday)
        )
        escalated = ServiceRequest.status == SR.ESCALATED
        stmt = (
            select(
                ServiceRequest.region_id,
                func.count().filter(open_),
                func.count().filter(booked),
                func.count().filter(done_week),
                func.count().filter(escalated),
            )
            .where(self.sr_scope(), ServiceRequest.region_id.is_not(None))
            .group_by(ServiceRequest.region_id)
        )
        counts = {row[0]: row[1:] for row in (await self.session.execute(stmt)).all()}
        regions = (
            await self.session.scalars(
                select(Region).where(Region.is_active).order_by(Region.sort_order, Region.id)
            )
        ).all()
        result: list[RegionLoad] = []
        for region in regions:
            if self.region_id is not None and region.id != self.region_id:
                continue
            if self.site_id is not None and region.supplying_site_id != self.site_id:
                continue
            o, b, c, e = counts.get(region.id, (0, 0, 0, 0))
            ref = region_ref(region)
            assert ref is not None
            result.append(
                RegionLoad(
                    region=ref,
                    open=int(o),
                    booked=int(b),
                    completed_this_week=int(c),
                    escalated=int(e),
                )
            )
        return result

    async def stock_alerts(self, limit: int = 10) -> list[StockAlert]:
        stmt = (
            select(ArticleSiteSetting)
            .join(Article, Article.id == ArticleSiteSetting.article_id)
            .join(Site, Site.id == ArticleSiteSetting.site_id)
            .where(
                Article.is_active,
                ArticleSiteSetting.demand_qty > ArticleSiteSetting.stock_qty,
                *self._site_filter(ArticleSiteSetting.site_id),
            )
            .order_by(
                (ArticleSiteSetting.demand_qty - ArticleSiteSetting.stock_qty).desc(),
                Article.number,
                Site.sort_order,
            )
            .limit(limit)
        )
        alerts: list[StockAlert] = []
        for setting in (await self.session.scalars(stmt)).all():
            article = article_ref(setting.article)
            site = site_ref(setting.site)
            assert article is not None
            assert site is not None
            alerts.append(
                StockAlert(
                    article=article,
                    site=site,
                    stock=setting.stock_qty,
                    demand=setting.demand_qty,
                    buffer=setting.buffer_qty,
                    order_qty=order_qty(setting.demand_qty, setting.stock_qty, setting.buffer_qty),
                )
            )
        return alerts


async def build_kpis(
    session: AsyncSession, site_id: int | None, region_id: int | None
) -> DashboardKpis:
    if site_id is not None and await session.get(Site, site_id) is None:
        raise ValidationProblem({"siteId": ["Unknown site."]})
    if region_id is not None and await session.get(Region, region_id) is None:
        raise ValidationProblem({"regionId": ["Unknown region."]})
    kpi = Kpi(session, site_id, region_id)
    return DashboardKpis(
        as_of=kpi.now,
        today=kpi.today,
        filters=DashboardFilters(site_id=site_id, region_id=region_id),
        tiles=await kpi.tiles(),
        stage_counts=await kpi.stage_counts(),
        series=await kpi.series(),
        region_load=await kpi.region_load(),
        stock_alerts=await kpi.stock_alerts(),
    )
