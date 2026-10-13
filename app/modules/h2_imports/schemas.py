"""H2 DTOs (API.md §5) — camelCase via ApiModel/ApiInput."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator

from app.core.enums import (
    ArticleCategory,
    DeliveryType,
    ImportJobStatus,
    ImportType,
    ProcurementType,
    RegionRuleMatchKind,
    Role,
)
from app.core.read_models.master import RegionOut
from app.core.schemas import (
    ApiInput,
    ApiModel,
    ContactRef,
    LocationRef,
    RegionRef,
    SiteRef,
    UserRef,
)

# --- imports -------------------------------------------------------------------------------


class ImportOptionsIn(ApiInput):
    dry_run: bool = False
    target_location_id: int | None = None
    mark_missing_as_probably_closed: bool = False


class ImportCounts(ApiModel):
    total: int | None
    processed: int
    created: int
    updated: int
    unchanged: int
    skipped: int
    errors: int
    warnings: int


class ImportJobSummary(ApiModel):
    id: int
    job_id: int
    type: ImportType
    type_detected: bool
    status: ImportJobStatus
    original_filename: str
    dry_run: bool
    counts: ImportCounts
    progress_percent: int | None
    created_by: UserRef
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    available_actions: list[str]


class ImportOptionsOut(ApiModel):
    dry_run: bool
    target_location_id: int | None
    mark_missing_as_probably_closed: bool
    target_location: LocationRef | None


class PostProcess(ApiModel):
    lease_returns_linked: int = 0
    hardware_hold: int = 0
    cancelled_by_itsm: int = 0
    probably_closed: int = 0
    reopened: int = 0
    contacts_created: int = 0
    contacts_updated: int = 0


class CodeMessage(ApiModel):
    code: str
    message: str


class TopErrorCode(ApiModel):
    code: str
    severity: Literal["error", "warning"]
    count: int


class ImportJobDetail(ImportJobSummary):
    options: ImportOptionsOut
    post_process: PostProcess
    job_level_warnings: list[CodeMessage]
    has_error_report: bool
    top_error_codes: list[TopErrorCode]


class ImportRowIssue(ApiModel):
    id: int
    row_number: int
    severity: Literal["error", "warning"]
    column_name: str | None
    code: str
    message: str
    raw_value: str | None
    business_key: str | None


class ImportDetection(ApiModel):
    detected_type: ImportType | None
    delimiter: Literal[",", ";"] | None
    headers: list[str]
    normalized_headers: list[str]
    missing_columns: list[str]
    extra_columns: list[str]
    row_count: int
    preview: list[dict[str, str | None]]
    problems: list[CodeMessage]


class TemplateColumn(ApiModel):
    name: str
    required: bool
    format: str
    description: str
    example: str | None


class TemplatePackedKey(ApiModel):
    key: str
    required: bool
    description: str


class TemplatePackedField(ApiModel):
    column: str
    keys: list[TemplatePackedKey]


class TemplateOption(ApiModel):
    name: str
    required: bool
    description: str


class ImportTemplate(ApiModel):
    type: ImportType
    label: str
    description: str
    signature_headers: list[str]
    upsert_key: str
    columns: list[TemplateColumn]
    packed_fields: list[TemplatePackedField]
    options: list[TemplateOption]


class GenerateSampleIn(ApiInput):
    type: ImportType
    rows: int = Field(ge=1, le=500)
    with_errors: bool = False


# --- articles ------------------------------------------------------------------------------


class ArticleInput(ApiInput):
    number: str = Field(min_length=1, max_length=16)
    category: ArticleCategory
    name: str = Field(min_length=1, max_length=120)
    model_number: str = Field(min_length=1, max_length=80)
    manufacturer: str = Field(min_length=1, max_length=80)
    external_part_number: str | None = Field(default=None, max_length=40)
    internal_description: str = Field(min_length=1, max_length=120)
    weight_kg: Decimal = Field(ge=0, le=999, max_digits=5, decimal_places=2)

    @field_validator(
        "number", "name", "model_number", "manufacturer", "internal_description", mode="before"
    )
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("external_part_number", mode="before")
    @classmethod
    def _blank_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class ArticlePatch(ApiInput):
    number: str | None = Field(default=None, min_length=1, max_length=16)
    category: ArticleCategory | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    model_number: str | None = Field(default=None, min_length=1, max_length=80)
    manufacturer: str | None = Field(default=None, min_length=1, max_length=80)
    external_part_number: str | None = Field(default=None, max_length=40)
    internal_description: str | None = Field(default=None, min_length=1, max_length=120)
    weight_kg: Decimal | None = Field(default=None, ge=0, le=999, max_digits=5, decimal_places=2)

    @field_validator(
        "number", "name", "model_number", "manufacturer", "internal_description", mode="before"
    )
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ArticleOut(ApiModel):
    id: int
    number: str
    category: ArticleCategory
    name: str
    model_number: str
    manufacturer: str
    external_part_number: str | None
    internal_description: str
    weight_kg: float
    is_active: bool
    asset_count: int
    open_demand: int


class ArticleSiteSettingOut(ApiModel):
    site: SiteRef
    buffer_qty: int
    min_qty: int | None
    max_qty: int | None
    stock_qty: int
    demand_qty: int
    calculated_at: datetime | None
    last_order_date: date | None
    last_ordered_qty: int | None


class ArticleDetail(ArticleOut):
    site_settings: list[ArticleSiteSettingOut]
    created_at: datetime
    updated_at: datetime


class SiteSettingPatch(ApiInput):
    min_qty: int | None = Field(ge=0, le=100_000)
    max_qty: int | None = Field(ge=0, le=100_000)


class EmptyBody(ApiInput):
    pass


# --- regions + rules -----------------------------------------------------------------------


class PickupPointIn(ApiInput):
    name: str = Field(min_length=1, max_length=120)
    street: str | None = Field(default=None, max_length=160)
    zip: str | None = Field(default=None, max_length=10)
    city: str | None = Field(default=None, max_length=80)


class RegionPatch(ApiInput):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    supplying_site_id: int | None = None
    default_delivery_type: DeliveryType | None = None
    booking_destination_id: str | None = Field(default=None, max_length=64)
    booking_service_id: str | None = Field(default=None, max_length=64)
    pickup_point: PickupPointIn | None = None
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=1000)


class RegionRuleInput(ApiInput):
    region_id: int
    match_kind: RegionRuleMatchKind
    pattern: str = Field(min_length=1, max_length=80)
    procurement_type: ProcurementType | None = None
    delivery_type: DeliveryType | None = None
    is_campus: bool = False
    priority: int = Field(default=0, ge=-100, le=100)
    is_active: bool = True
    note: str | None = Field(default=None, max_length=200)


class RegionRulePatch(ApiInput):
    region_id: int | None = None
    match_kind: RegionRuleMatchKind | None = None
    pattern: str | None = Field(default=None, min_length=1, max_length=80)
    procurement_type: ProcurementType | None = None
    delivery_type: DeliveryType | None = None
    is_campus: bool | None = None
    priority: int | None = Field(default=None, ge=-100, le=100)
    is_active: bool | None = None
    note: str | None = Field(default=None, max_length=200)


class RegionRuleOut(ApiModel):
    id: int
    region_id: int
    match_kind: RegionRuleMatchKind
    pattern: str
    procurement_type: ProcurementType | None
    delivery_type: DeliveryType | None
    is_campus: bool
    priority: int
    is_active: bool
    note: str | None
    normalized_pattern: str
    region: RegionRef
    match_count: int


class RegionDetail(RegionOut):
    rules: list[RegionRuleOut]
    booking_link_example: str | None


class RegionTestIn(ApiInput):
    zip: str | None = Field(default=None, max_length=20)
    city: str | None = Field(default=None, max_length=80)
    cost_center: str | None = Field(default=None, max_length=20)
    procurement_type: ProcurementType | None = None


class NormalizedInputOut(ApiModel):
    zip: str | None
    city: str | None
    cost_center: str | None


class RankedRule(ApiModel):
    rule: RegionRuleOut
    rank: int


class RegionResolutionResult(ApiModel):
    resolved: bool
    region: RegionRef | None
    rule: RegionRuleOut | None
    delivery_type: DeliveryType | None
    is_campus: bool
    supplying_site: SiteRef | None
    normalized_input: NormalizedInputOut
    candidates: list[RankedRule]


# --- VIP list ------------------------------------------------------------------------------


class VipEntryInput(ApiInput):
    user_id: str = Field(min_length=3, max_length=16, pattern=r"^[A-Za-z0-9]{3,16}$")
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    vip_type: str = Field(min_length=1, max_length=40)
    title: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)


class VipEntryPatch(ApiInput):
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, min_length=1, max_length=80)
    vip_type: str | None = Field(default=None, min_length=1, max_length=40)
    title: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)


class VipEntryOut(ApiModel):
    id: int
    user_id: str
    first_name: str | None
    last_name: str
    vip_type: str
    title: str | None
    note: str | None
    contact: ContactRef | None
    open_service_requests: int
    open_lease_returns: int
    created_at: datetime


# --- users ---------------------------------------------------------------------------------


class UserCreate(ApiInput):
    email: str = Field(min_length=3, max_length=254)
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    role: Role
    home_site_id: int | None = None
    password: str = Field(min_length=8, max_length=128)


class UserPatch(ApiInput):
    first_name: str | None = Field(default=None, min_length=1, max_length=80)
    last_name: str | None = Field(default=None, min_length=1, max_length=80)
    role: Role | None = None
    home_site_id: int | None = None


class ResetPasswordIn(ApiInput):
    new_password: str = Field(min_length=8, max_length=128)
