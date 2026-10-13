"""H4 DTOs (API.md §7, SPEC_RESOLUTIONS #21). SR / contact shapes come from core builders."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import EmailStr, Field

from app.core.enums import (
    AppointmentStatus,
    DeliveryType,
    ItemSlot,
    OsReinstallTiming,
    ProcurementType,
    Readiness,
    ServiceRequestStatus,
    TourStatus,
    TourStopOutcome,
)
from app.core.read_models import ContactDetail, ServiceRequestDetail, ServiceRequestListItem
from app.core.read_models.master import DeviationCodeOut
from app.core.schemas import (
    ApiInput,
    ApiModel,
    ContactRef,
    DeliveryAddress,
    RegionRef,
    UserRef,
)

# --- calendars + appointments (API.md §7.1) ---------------------------------------------------


class CalendarRef(ApiModel):
    id: int
    name: str


class CalendarWithRegion(CalendarRef):
    region: RegionRef | None


class BookingCalendarOut(ApiModel):
    id: int
    external_id: str
    name: str
    region: RegionRef | None
    service_ids: list[str]
    calendar_type: str | None
    is_enabled: bool
    synced_at: dt.datetime | None
    upcoming_appointments: int


class BookingCalendarPatch(ApiInput):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    region_id: int | None = None
    is_enabled: bool | None = None


class AppointmentSrRef(ApiModel):
    id: int
    number: str
    status: ServiceRequestStatus


class AppointmentOut(ApiModel):
    id: int
    external_id: str
    calendar: CalendarRef
    service_request: AppointmentSrRef | None
    reference: str | None
    status: AppointmentStatus
    start_at: dt.datetime
    end_at: dt.datetime
    title: str | None
    contact_name: str | None
    contact_email: str | None
    street: str | None
    town: str | None
    notes: str | None
    booked_at: dt.datetime | None
    source: Literal["sync", "manual"]
    is_current: bool


class AppointmentLinkBody(ApiInput):
    service_request_number: str = Field(min_length=1, max_length=20)


class SyncCounts(ApiModel):
    calendars: int = 0
    created: int = 0
    updated: int = 0
    cancelled: int = 0
    matched: int = 0
    unmatched: int = 0
    transitions: int = 0


class SyncStateOut(ApiModel):
    running: bool
    last_job_id: int | None
    last_incremental_sync_at: dt.datetime | None
    last_full_sync_at: dt.datetime | None
    last_result: SyncCounts | None
    next_scheduled_at: dt.datetime


# --- tours (API.md §7.2) ---------------------------------------------------------------------


class TourCreate(ApiInput):
    calendar_id: int
    tour_date: dt.date
    technician_id: int | None = None
    service_request_ids: list[int] = Field(default_factory=list, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)


class TourPatch(ApiInput):
    technician_id: int | None = None
    notes: str | None = Field(default=None, max_length=2000)
    version: int | None = None


class TourStopsPut(ApiInput):
    service_request_ids: list[int] = Field(max_length=60)
    version: int | None = None


class TourActionBody(ApiInput):
    comment: str | None = Field(default=None, max_length=2000)
    version: int | None = None


class TourSummaryOut(ApiModel):
    id: int
    calendar: CalendarWithRegion
    tour_date: dt.date
    technician: UserRef | None
    status: TourStatus
    stop_count: int
    pending_count: int
    completed_count: int
    rescheduled_count: int
    cancelled_count: int
    first_appointment_at: dt.datetime | None
    last_appointment_at: dt.datetime | None
    readiness: dict[Readiness, int]
    available_actions: list[str]


class StopItem(ApiModel):
    slot: ItemSlot
    label: str
    verified: bool


class StopContact(ContactRef):
    phone: str | None
    mobile: str | None


class StopServiceRequest(ApiModel):
    id: int
    number: str
    status: ServiceRequestStatus
    delivery_type: DeliveryType | None
    is_campus: bool
    is_vip: bool
    contact: StopContact
    onsite_contact_phone: str | None
    delivery_address: DeliveryAddress
    appointment_start_at: dt.datetime | None
    appointment_end_at: dt.datetime | None
    new_items: list[StopItem]
    old_items: list[StopItem]


class TourStopOut(ApiModel):
    id: int
    position: int
    outcome: TourStopOutcome
    outcome_at: dt.datetime | None
    readiness: Readiness
    service_request: StopServiceRequest


class TourDetailOut(TourSummaryOut):
    notes: str | None
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    stops: list[TourStopOut]
    warnings: list[str]
    version: int


class TourCandidate(ApiModel):
    service_request: ServiceRequestListItem
    readiness: Readiness
    in_tour_id: int | None


class TourCandidatesOut(ApiModel):
    calendar: CalendarWithRegion
    date: dt.date
    tour: TourSummaryOut | None
    candidates: list[TourCandidate]


class OverviewTour(ApiModel):
    id: int
    status: TourStatus
    technician: UserRef | None


class OverviewCell(ApiModel):
    date: dt.date
    appointment_count: int
    planned_count: int
    tour: OverviewTour | None


class OverviewRow(ApiModel):
    calendar: CalendarWithRegion
    cells: list[OverviewCell]


class TechnicianDay(ApiModel):
    date: dt.date
    tour_count: int
    stop_count: int


class TechnicianLoad(ApiModel):
    technician: UserRef
    days: list[TechnicianDay]


class TourPlannerOverviewOut(ApiModel):
    days: list[dt.date]
    rows: list[OverviewRow]
    technicians: list[TechnicianLoad]


class TourStopRef(ApiModel):
    """``GET /service-requests/{id}/tour-stops`` item (SPEC_RESOLUTIONS #21)."""

    tour_id: int
    stop_id: int
    tour_date: dt.date
    technician_name: str | None
    status: TourStatus
    outcome: TourStopOutcome


# --- deployment (API.md §7.3) ----------------------------------------------------------------


class WorkInstructions(ApiModel):
    procurement_type: ProcurementType | None
    scope_of_delivery: str | None
    client_component: str | None
    os_reinstall: OsReinstallTiming | None
    without_asset_prep: bool
    software: list[str]
    note: str | None
    onsite_contact_phone: str | None
    delivery_address: DeliveryAddress
    is_campus: bool


class DeploymentProgress(ApiModel):
    new_verified: int
    new_total: int
    old_verified: int
    old_total: int


class DeploymentViewOut(ApiModel):
    tour: TourSummaryOut
    stop: TourStopOut
    service_request: ServiceRequestDetail
    contact: ContactDetail
    work_instructions: WorkInstructions
    deviation_codes: list[DeviationCodeOut]
    progress: DeploymentProgress


class DeploymentScanBody(ApiInput):
    service_request_id: int
    serial: str = Field(min_length=1, max_length=80)


class RegisterUnknownBody(ApiInput):
    serial: str = Field(min_length=1, max_length=40)
    asset_tag: str | None = Field(default=None, max_length=20)
    model: str | None = Field(default=None, max_length=80)


class ProtocolSendBody(ApiInput):
    email: EmailStr | None = None


class ProtocolQueued(ApiModel):
    notification_id: str
