"""H3 request / response DTOs (API.md §6). SR, item and asset shapes come from core builders."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator

from app.core.enums import ItemSlot, OsReinstallTiming
from app.core.read_models import (
    ServiceRequestDetail,
    ServiceRequestItemOut,
    ServiceRequestListItem,
)
from app.core.schemas import (
    Address,
    ApiInput,
    ApiModel,
    ArticleRef,
    AssetSummary,
    DownloadLink,
    SiteRef,
    SrRef,
    UserRef,
)

# --- installation place scan station (API.md §6.1) -------------------------------------------


class InstallationPlaceScanIn(ApiInput):
    site_id: int
    serial: str = Field(min_length=1, max_length=60)
    place: str = Field(min_length=1, max_length=20)


class InstallationPlaceUndoIn(ApiInput):
    movement_id: int


class InstallationPlaceOccupancy(ApiModel):
    place: str
    assets: list[AssetSummary]


# --- personalization (API.md §6.2) ------------------------------------------------------------


class StockHint(ApiModel):
    slot: ItemSlot
    article: ArticleRef | None
    available_count: int
    suggested: AssetSummary | None


class PersonalizationQueueItem(ServiceRequestListItem):
    items: list[ServiceRequestItemOut]
    stock_hints: list[StockHint]
    all_new_items_assigned: bool


class AssetCandidate(ApiModel):
    asset: AssetSummary
    checked_in_at: datetime | None
    age_days: int | None
    fifo_rank: int


class AssetCandidates(ApiModel):
    slot: ItemSlot
    article: ArticleRef | None
    site: SiteRef
    suggested_asset_id: int | None
    data: list[AssetCandidate]


class AssignmentIn(ApiInput):
    serial: str = Field(min_length=1, max_length=60)


class StagingPatch(ApiInput):
    """``PATCH /service-requests/{id}/staging``.

    API.md defines ``osReinstall`` (+ ``version``); ``addedSoftware`` is accepted as a
    convenience so the work panel can save both options in one call (same rules as
    ``PUT …/software-checklist``). Omitted fields stay unchanged.
    """

    os_reinstall: OsReinstallTiming | None = None
    added_software: list[str] | None = Field(default=None, max_length=50)
    version: int | None = None

    @field_validator("added_software")
    @classmethod
    def _entries(cls, value: list[str] | None) -> list[str] | None:
        return _check_entries(value)


class SoftwareChecklistOut(ApiModel):
    required: list[str]
    installed: list[str]
    added: list[str]
    missing: list[str]
    complete: bool


class SoftwareChecklistIn(ApiInput):
    installed_software: list[str] = Field(max_length=50)
    added_software: list[str] = Field(max_length=50)

    @field_validator("installed_software", "added_software")
    @classmethod
    def _entries(cls, value: list[str]) -> list[str]:
        checked = _check_entries(value)
        assert checked is not None
        return checked


def _check_entries(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    for entry in value:
        if len(entry.strip()) > 120:
            raise ValueError("Each software entry may have at most 120 characters.")
    return value


# --- order closing (API.md §6.3) ---------------------------------------------------------------


class OrderClosingQueueItem(ServiceRequestListItem):
    items: list[ServiceRequestItemOut]
    checklist: SoftwareChecklistOut
    os_reinstall: OsReinstallTiming | None


# --- picking (API.md §6.4 / §6.5) --------------------------------------------------------------


class PickingCalendar(ApiModel):
    id: int | None
    name: str | None


class TechnicianPickingRow(ServiceRequestListItem):
    items: list[ServiceRequestItemOut]
    picked: bool


class TechnicianPickingGroup(ApiModel):
    calendar: PickingCalendar
    service_requests: list[TechnicianPickingRow]


class PickingTotals(ApiModel):
    to_pick: int
    picked: int


class TechnicianPicking(ApiModel):
    date: date
    site: SiteRef
    groups: list[TechnicianPickingGroup]
    totals: PickingTotals


class ShipTo(ApiModel):
    kind: Literal["user", "pickup_point"]
    recipient_name: str
    name2: str | None
    address: Address


class ParcelPickingItem(ServiceRequestListItem):
    items: list[ServiceRequestItemOut]
    all_packed: bool
    has_old_items: bool
    ship_to: ShipTo


class ParcelScanIn(ApiInput):
    service_request_id: int
    serial: str = Field(min_length=1, max_length=60)


# --- shipments (API.md §6.6) --------------------------------------------------------------------


class ShipmentCreate(ApiInput):
    include_return_label: bool = True
    combined_parcel: bool = False
    send_confirmation: bool = True
    version: int | None = None


class ShipmentOut(ApiModel):
    id: int
    service_request: SrRef
    carrier_shipment_id: str
    parcel_count: int
    tracking_urls: list[str]
    return_tracking_urls: list[str]
    include_return_label: bool
    combined_parcel: bool
    confirmation_mail_sent: bool
    recipient_name: str
    delivery_address: Address
    created_by: UserRef
    created_at: datetime


class ShipmentCreated(ApiModel):
    shipment: ShipmentOut
    service_request: ServiceRequestDetail
    label: DownloadLink
    warnings: list[str]
