"""H5 DTOs (API.md §8) — camelCase via ``ApiModel`` / ``ApiInput``."""

from __future__ import annotations

from datetime import date, datetime
from datetime import date as Date  # noqa: N812 - field named ``date`` below
from typing import Literal

from pydantic import EmailStr, Field, field_validator

from app.core.actions import ActionBody
from app.core.enums import (
    AssetStatus,
    CallOffOrderStatus,
    DeactivationReason,
    InventoryScanResult,
    InventoryStatus,
    PipelineStage,
)
from app.core.pagination import PageMeta
from app.core.schemas import (
    ApiInput,
    ApiModel,
    ArticleRef,
    AssetSummary,
    DownloadLink,
    EntityRef,
    LocationRef,
    RegionRef,
    SiteRef,
    UserRef,
)

# --- stock matrix (API.md §8.1) -------------------------------------------------------------


class StockCell(ApiModel):
    site_id: int
    goods_in: int
    staging: int
    goods_out: int
    stock: int
    demand: int
    buffer: int
    order_qty: int
    min_qty: int | None
    max_qty: int | None
    below_min: bool
    above_max: bool
    last_order_date: date | None
    last_ordered_qty: int | None


class StockCellWithArticle(StockCell):
    article_id: int


class StockArticleRef(ArticleRef):
    is_active: bool


class StockRowTotals(ApiModel):
    stock: int
    demand: int
    order_qty: int


class StockRow(ApiModel):
    article: StockArticleRef
    cells: list[StockCell]
    totals: StockRowTotals


class UnmatchedDemand(ApiModel):
    requested_type: str
    site_id: int | None
    count: int


class StockMatrixMeta(ApiModel):
    unmatched_demand: list[UnmatchedDemand]
    unresolved_demand: int
    recalculating: bool


class StockMatrix(ApiModel):
    calculated_at: datetime | None
    sites: list[SiteRef]
    rows: list[StockRow]
    meta: StockMatrixMeta


class BufferEntry(ApiInput):
    article_id: int
    buffer_qty: int = Field(ge=0, le=100_000)


class BuffersPatch(ApiInput):
    site_id: int
    buffers: list[BufferEntry] = Field(min_length=1, max_length=500)


class StockCellList(ApiModel):
    data: list[StockCellWithArticle]


# --- call-off orders (API.md §8.2) ----------------------------------------------------------

type LineWarning = Literal["below_min", "above_max"]


class CallOffOrderSummary(ApiModel):
    id: int
    number: str
    site: SiteRef
    status: CallOffOrderStatus
    delivery_date: date
    line_count: int
    total_quantity: int
    recipient_email: str
    created_by: UserRef
    created_at: datetime
    sent_at: datetime | None
    sent_by: UserRef | None


class CallOffLine(ApiModel):
    id: int
    article: ArticleRef
    demand_qty: int
    stock_qty: int
    buffer_qty: int
    suggested_qty: int
    quantity: int
    min_qty: int | None
    max_qty: int | None
    warnings: list[LineWarning]


class CallOffOrderOut(CallOffOrderSummary):
    note: str | None
    lines: list[CallOffLine]
    warnings: list[str]
    available_actions: list[str]
    version: int
    mail_log_entity_ref: EntityRef


class CallOffOrderCreate(ApiInput):
    site_id: int
    delivery_date: date
    note: str | None = Field(default=None, max_length=2000)


class CallOffLinePatch(ApiInput):
    article_id: int
    quantity: int = Field(ge=0, le=100_000)


class CallOffOrderPatch(ApiInput):
    version: int | None = None
    delivery_date: date | None = None
    note: str | None = Field(default=None, max_length=2000)
    recipient_email: EmailStr | None = None
    lines: list[CallOffLinePatch] | None = Field(default=None, max_length=500)


class CallOffSendBody(ApiInput):
    version: int | None = None


class CallOffCancelBody(ApiInput):
    comment: str | None = Field(default=None, max_length=2000)
    version: int | None = None


# --- assets (API.md §8.3) --------------------------------------------------------------------


class AssetPatch(ApiInput):
    version: int | None = None
    asset_tag: str | None = Field(default=None, max_length=20)
    mac_address: str | None = Field(default=None, max_length=17)
    external_asset_id: str | None = Field(default=None, max_length=40)
    ci_name: str | None = Field(default=None, max_length=80)


class MarkMissingBody(ActionBody):
    reason: str | None = Field(default=None, max_length=2000)


class MarkFoundBody(ActionBody):
    location_id: int
    installation_place: str | None = Field(default=None, max_length=12)


class CorrectBody(ActionBody):
    status: AssetStatus | None = None
    location_id: int | None = None
    installation_place: str | None = Field(default=None, max_length=12)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A reason is required for corrections.")
        return value.strip()


# --- deactivation (API.md §8.4) ----------------------------------------------------------------

type DeactivationOutcome = Literal["deactivated", "already_deactivated", "rejected", "not_found"]


class DeactivationBatchOut(ApiModel):
    id: int
    reason: DeactivationReason
    original_filename: str
    total_rows: int
    deactivated_count: int
    already_deactivated_count: int
    rejected_count: int
    not_found_count: int
    created_by: UserRef
    created_at: datetime


class DeactivationSampleRow(ApiModel):
    serial_number: str
    outcome: DeactivationOutcome
    detail: str | None


class DeactivationResult(DeactivationBatchOut):
    not_found_report: DownloadLink | None
    rejected_report: DownloadLink | None
    closed_lease_returns: int
    sample: list[DeactivationSampleRow]


# --- inventory (API.md §8.5) -------------------------------------------------------------------

type ReconciliationCategory = Literal["matched", "missing", "wrong_location", "unknown"]


class InventoryCounts(ApiModel):
    expected: int
    matched: int
    missing: int
    wrong_location: int
    unknown: int
    corrected: int | None


class InventorySummary(ApiModel):
    id: int
    site: SiteRef
    status: InventoryStatus
    opened_at: datetime
    opened_by: UserRef
    closed_at: datetime | None
    closed_by: UserRef | None
    count_date: date | None
    summary: InventoryCounts


class InventoryScanOut(ApiModel):
    id: int
    serial_number: str
    result: InventoryScanResult
    asset: AssetSummary | None
    scanned_location: LocationRef | None
    recorded_location: LocationRef | None
    scanned_by: UserRef
    scanned_at: datetime


class InventoryProgress(ApiModel):
    scanned: int
    expected: int
    percent: int


class InventoryDetail(InventorySummary):
    note: str | None
    apply_corrections: bool | None
    version: int
    progress: InventoryProgress
    zones: list[LocationRef]
    recent_scans: list[InventoryScanOut]
    available_actions: list[str]


class ReconciliationItem(ApiModel):
    serial_number: str
    asset: AssetSummary | None
    category: ReconciliationCategory
    recorded_location: LocationRef | None
    scanned_location: LocationRef | None
    scanned_by: UserRef | None
    scanned_at: datetime | None
    correctable: bool


class Reconciliation(ApiModel):
    data: list[ReconciliationItem]
    meta: PageMeta
    summary: InventoryCounts


class InventoryCreate(ApiInput):
    site_id: int
    note: str | None = Field(default=None, max_length=2000)


class InventoryScanBody(ApiInput):
    serial: str = Field(min_length=1, max_length=64)
    stock_location_id: int | None = None


class InventoryCloseBody(ApiInput):
    count_date: date
    apply_corrections: bool
    version: int | None = None


class InventoryCancelBody(ApiInput):
    comment: str | None = Field(default=None, max_length=2000)
    version: int | None = None


# --- dashboard (API.md §8.6) -------------------------------------------------------------------


class DashboardFilters(ApiModel):
    site_id: int | None
    region_id: int | None


class DashboardTiles(ApiModel):
    open_service_requests: int
    needs_attention: int
    appointments_today: int
    appointments_this_week: int
    completed_this_week: int
    completed_last_week: int
    escalated_service_requests: int
    escalated_lease_returns: int
    hardware_unavailable: int
    reschedule_needed: int
    lease_returns_due_next_30_days: int
    lease_returns_overdue: int
    stock_shortfall_articles: int
    open_inventories: int
    chain_mails_sent_today: int
    avg_days_import_to_completion_30d: float | None = Field(
        serialization_alias="avgDaysImportToCompletion30d"
    )
    last_import_at: datetime | None
    last_appointment_sync_at: datetime | None
    last_stock_calculation_at: datetime | None


class StageCount(ApiModel):
    stage: PipelineStage
    count: int


class DayCount(ApiModel):
    date: Date
    count: int


class DashboardSeries(ApiModel):
    completions_per_day: list[DayCount]
    appointments_per_day: list[DayCount]
    imported_per_day: list[DayCount]


class RegionLoad(ApiModel):
    region: RegionRef
    open: int
    booked: int
    completed_this_week: int
    escalated: int


class StockAlert(ApiModel):
    article: ArticleRef
    site: SiteRef
    stock: int
    demand: int
    buffer: int
    order_qty: int


class DashboardKpis(ApiModel):
    as_of: datetime
    today: date
    filters: DashboardFilters
    tiles: DashboardTiles
    stage_counts: list[StageCount]
    series: DashboardSeries
    region_load: list[RegionLoad]
    stock_alerts: list[StockAlert]
