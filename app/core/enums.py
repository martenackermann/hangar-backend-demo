"""All enums of the spec (core and slice-owned, decision D-17) with UI metadata.

Values are the snake_case strings used in the DB and the API. :data:`ENUM_META` carries
labels, tones (DESIGN.md tone families), pipeline stages and terminal flags; it feeds
``GET /meta/enums``. Slices import enums from here — never define a parallel enum.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast


class Tone(StrEnum):
    INTAKE = "intake"
    OUTREACH = "outreach"
    BOOKED = "booked"
    WORKSHOP = "workshop"
    TRANSIT = "transit"
    DONE = "done"
    CAUTION = "caution"
    CRITICAL = "critical"


class Trigger(StrEnum):
    USER = "user"
    JOB = "job"
    IMPORT = "import"
    SYNC = "sync"
    SYSTEM = "system"


class Role(StrEnum):
    VIEWER = "viewer"
    TECHNICIAN = "technician"
    DISPATCHER = "dispatcher"
    ADMIN = "admin"


class PipelineStage(StrEnum):
    IMPORTED = "imported"
    INVITED = "invited"
    BOOKED = "booked"
    PERSONALIZING = "personalizing"
    READY = "ready"
    DELIVERY = "delivery"
    DEPLOYED = "deployed"
    CLOSED = "closed"


class ServiceRequestStatus(StrEnum):
    IMPORTED = "imported"
    IMPORTED_VIP = "imported_vip"
    HARDWARE_UNAVAILABLE = "hardware_unavailable"
    INVITED = "invited"
    CAMPUS_INFO_SENT = "campus_info_sent"
    REMINDER_1_SENT = "reminder_1_sent"
    REMINDER_2_SENT = "reminder_2_sent"
    ESCALATED = "escalated"
    APPOINTMENT_BOOKED = "appointment_booked"
    PERSONALIZATION_IN_PROGRESS = "personalization_in_progress"
    PERSONALIZATION_DONE = "personalization_done"
    READY_FOR_PICKING = "ready_for_picking"
    SHIPPED = "shipped"
    PROVIDED = "provided"
    RESCHEDULE_NEEDED = "reschedule_needed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class LeaseReturnStatus(StrEnum):
    IMPORTED = "imported"
    IMPORTED_VIP = "imported_vip"
    INFO_SENT = "info_sent"
    REMINDER_1_SENT = "reminder_1_sent"
    REMINDER_2_SENT = "reminder_2_sent"
    ESCALATED = "escalated"
    REPLACEMENT_ORDERED = "replacement_ordered"
    PROBABLY_CLOSED = "probably_closed"
    CLOSED = "closed"


class AssetStatus(StrEnum):
    AVAILABLE = "available"
    ASSIGNED = "assigned"
    PREPARED = "prepared"
    PROVIDED = "provided"
    IN_USE = "in_use"
    COLLECTED = "collected"
    MISSING = "missing"
    DEACTIVATED_RETURNED = "deactivated_returned"
    DEACTIVATED_BOUGHT_OUT = "deactivated_bought_out"
    DEACTIVATED_DISPOSED = "deactivated_disposed"


class LocationType(StrEnum):
    GOODS_IN = "goods_in"
    STAGING = "staging"
    GOODS_OUT = "goods_out"
    RETURN = "return"
    IN_TRANSIT = "in_transit"
    IN_USE = "in_use"
    OFF_FLEET = "off_fleet"


class AssetAction(StrEnum):
    RECEIVE = "receive"
    PLACE = "place"
    ASSIGN = "assign"
    UNASSIGN = "unassign"
    PREPARE = "prepare"
    PROVIDE = "provide"
    RETURN_TO_HUB = "return_to_hub"
    RELEASE = "release"
    DEPLOY = "deploy"
    REGISTER_UNKNOWN = "register_unknown"
    COLLECT = "collect"
    SYNC_IN_USE = "sync_in_use"
    SYNC_COLLECTED = "sync_collected"
    MARK_MISSING = "mark_missing"
    MARK_FOUND = "mark_found"
    INVENTORY_RELOCATE = "inventory_relocate"
    DEACTIVATE = "deactivate"
    CORRECT = "correct"


class DeliveryType(StrEnum):
    TECHNICIAN = "technician"
    PARCEL_TO_USER = "parcel_to_user"
    PARCEL_TO_PICKUP_POINT = "parcel_to_pickup_point"


class ProcurementType(StrEnum):
    REPLACEMENT = "replacement"
    NEW_HIRE = "new_hire"
    ADDITIONAL_DEVICE = "additional_device"
    REMOVAL = "removal"


class ItemSlot(StrEnum):
    NEW_BASE = "new_base"
    NEW_PERIPHERAL_1 = "new_peripheral_1"
    NEW_PERIPHERAL_2 = "new_peripheral_2"
    OLD_BASE = "old_base"
    OLD_PERIPHERAL_1 = "old_peripheral_1"
    OLD_PERIPHERAL_2 = "old_peripheral_2"

    @property
    def is_new(self) -> bool:
        return self.value.startswith("new_")

    @property
    def direction(self) -> str:
        return "deliver" if self.is_new else "collect"


class OsReinstallTiming(StrEnum):
    ON_PICKUP = "on_pickup"
    ON_DELIVERY = "on_delivery"


class DeactivationReason(StrEnum):
    RETURNED_TO_LESSOR = "returned_to_lessor"
    BOUGHT_OUT = "bought_out"
    DISPOSED = "disposed"


class Gender(StrEnum):
    FEMALE = "female"
    MALE = "male"
    DIVERSE = "diverse"
    UNSPECIFIED = "unspecified"


class SiteKind(StrEnum):
    HUB = "hub"
    CAMPUS = "campus"


class RegionRuleMatchKind(StrEnum):
    COST_CENTER = "cost_center"
    ZIP = "zip"
    ZIP_PREFIX = "zip_prefix"
    CITY = "city"


class ArticleCategory(StrEnum):
    LAPTOP = "laptop"
    DESKTOP = "desktop"
    TABLET = "tablet"
    DOCKING_STATION = "docking_station"
    MONITOR = "monitor"
    HEADSET = "headset"
    KEYBOARD = "keyboard"
    MOUSE = "mouse"
    WEBCAM = "webcam"
    BAG = "bag"
    ACCESSORY = "accessory"


class ImportType(StrEnum):
    LEASE_RETURNS = "lease_returns"
    SERVICE_REQUESTS = "service_requests"
    CONTACTS = "contacts"
    ASSETS_NEW = "assets_new"
    ASSET_STATUS_SYNC = "asset_status_sync"


class ImportJobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InventoryStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class InventoryScanResult(StrEnum):
    MATCHED = "matched"
    WRONG_LOCATION = "wrong_location"
    UNKNOWN = "unknown"


class TourStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TourStopOutcome(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"


class CallOffOrderStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    CANCELLED = "cancelled"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(StrEnum):
    IMPORT = "import"
    APPOINTMENTS_SYNC = "appointments_sync"
    STOCK_RECALCULATE = "stock_recalculate"
    COMMS_DISPATCH = "comms_dispatch"
    REGIONS_REAPPLY = "regions_reapply"
    DEMO_RESET = "demo_reset"


class DeviationCategory(StrEnum):
    PROVIDER_RESCHEDULE = "provider_reschedule"
    CUSTOMER_RESCHEDULE = "customer_reschedule"
    REFUSAL = "refusal"
    EXTRA_EFFORT = "extra_effort"
    CANCELLATION = "cancellation"
    SITE_ISSUE = "site_issue"


class DeviationOutcome(StrEnum):
    COMPLETE = "complete"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"


class MailStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"


class AppointmentStatus(StrEnum):
    BOOKED = "booked"
    CANCELLED = "cancelled"


class ScanVerdict(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


class AssetSource(StrEnum):
    IMPORT_NEW = "import_new"
    IMPORT_STATUS_SYNC = "import_status_sync"
    TECHNICIAN_SCAN = "technician_scan"
    MANUAL = "manual"
    SEED = "seed"


class Readiness(StrEnum):
    READY = "ready"
    TO_PICK = "to_pick"
    IN_STAGING = "in_staging"
    NOT_STARTED = "not_started"


class HoldSource(StrEnum):
    """Who put an SR on hardware hold (SPEC_RESOLUTIONS #37)."""

    IMPORT = "import"
    MANUAL = "manual"


class AttentionFlag(StrEnum):
    REGION_UNRESOLVED = "region_unresolved"
    CONTACT_EMAIL_MISSING = "contact_email_missing"
    UNKNOWN_ARTICLE = "unknown_article"
    APPOINTMENT_SOON_NOT_READY = "appointment_soon_not_ready"
    APPOINTMENT_OVERDUE = "appointment_overdue"
    ESCALATED_STALE = "escalated_stale"
    ITSM_CLOSED = "itsm_closed"
    HOLD_GROUP_LEFT = "hold_group_left"
    RETIREMENT_PASSED = "retirement_passed"
    NOT_COLLECTED = "not_collected"


# --- status groups (SPEC §4) -----------------------------------------------------------

SR = ServiceRequestStatus
LR = LeaseReturnStatus

SR_TERMINAL: frozenset[SR] = frozenset({SR.COMPLETED, SR.CANCELLED})
SR_NON_TERMINAL: frozenset[SR] = frozenset(set(SR) - SR_TERMINAL)
PRE_APPT: frozenset[SR] = frozenset(
    {
        SR.IMPORTED,
        SR.IMPORTED_VIP,
        SR.INVITED,
        SR.CAMPUS_INFO_SENT,
        SR.REMINDER_1_SENT,
        SR.REMINDER_2_SENT,
        SR.ESCALATED,
        SR.RESCHEDULE_NEEDED,
    }
)
WITH_APPT: frozenset[SR] = frozenset(
    {
        SR.APPOINTMENT_BOOKED,
        SR.PERSONALIZATION_IN_PROGRESS,
        SR.PERSONALIZATION_DONE,
        SR.READY_FOR_PICKING,
        SR.PROVIDED,
        SR.SHIPPED,
    }
)
#: SR statuses whose new items count as demand (SPEC §6.7); plus reschedule_needed unless
#: its resume status is ``shipped``.
DEMAND_STATUSES: frozenset[SR] = frozenset(
    {
        SR.IMPORTED,
        SR.IMPORTED_VIP,
        SR.INVITED,
        SR.CAMPUS_INFO_SENT,
        SR.REMINDER_1_SENT,
        SR.REMINDER_2_SENT,
        SR.ESCALATED,
        SR.APPOINTMENT_BOOKED,
        SR.PERSONALIZATION_IN_PROGRESS,
        SR.PERSONALIZATION_DONE,
        SR.READY_FOR_PICKING,
    }
)
#: Statuses in which region / items may still change (SPEC_RESOLUTIONS #29): everything
#: before ``appointment_booked`` incl. ``hardware_unavailable``, excluding ``reschedule_needed``.
REGION_MUTABLE_STATUSES: frozenset[SR] = frozenset(
    {
        SR.IMPORTED,
        SR.IMPORTED_VIP,
        SR.HARDWARE_UNAVAILABLE,
        SR.INVITED,
        SR.CAMPUS_INFO_SENT,
        SR.REMINDER_1_SENT,
        SR.REMINDER_2_SENT,
        SR.ESCALATED,
    }
)
LR_TERMINAL: frozenset[LR] = frozenset({LR.CLOSED})
LR_NON_TERMINAL: frozenset[LR] = frozenset(set(LR) - LR_TERMINAL)
DEACTIVATED: frozenset[AssetStatus] = frozenset(
    {
        AssetStatus.DEACTIVATED_RETURNED,
        AssetStatus.DEACTIVATED_BOUGHT_OUT,
        AssetStatus.DEACTIVATED_DISPOSED,
    }
)
DEACTIVATION_STATUS: dict[DeactivationReason, AssetStatus] = {
    DeactivationReason.RETURNED_TO_LESSOR: AssetStatus.DEACTIVATED_RETURNED,
    DeactivationReason.BOUGHT_OUT: AssetStatus.DEACTIVATED_BOUGHT_OUT,
    DeactivationReason.DISPOSED: AssetStatus.DEACTIVATED_DISPOSED,
}
STOCK_TYPES: frozenset[LocationType] = frozenset(
    {LocationType.GOODS_IN, LocationType.STAGING, LocationType.GOODS_OUT}
)
GLOBAL_LOCATION_TYPES: frozenset[LocationType] = frozenset(
    {LocationType.IN_TRANSIT, LocationType.IN_USE, LocationType.OFF_FLEET}
)
NEW_SLOTS: tuple[ItemSlot, ...] = (
    ItemSlot.NEW_BASE,
    ItemSlot.NEW_PERIPHERAL_1,
    ItemSlot.NEW_PERIPHERAL_2,
)
OLD_SLOTS: tuple[ItemSlot, ...] = (
    ItemSlot.OLD_BASE,
    ItemSlot.OLD_PERIPHERAL_1,
    ItemSlot.OLD_PERIPHERAL_2,
)
PARCEL_DELIVERY: frozenset[DeliveryType] = frozenset(
    {DeliveryType.PARCEL_TO_USER, DeliveryType.PARCEL_TO_PICKUP_POINT}
)


# --- UI metadata -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnumMeta:
    label: str
    tone: Tone | None = None
    description: str | None = None
    stage: PipelineStage | None = None
    terminal: bool | None = None


T = Tone
_I, _O, _B, _W, _TR, _D, _C, _X = (
    T.INTAKE,
    T.OUTREACH,
    T.BOOKED,
    T.WORKSHOP,
    T.TRANSIT,
    T.DONE,
    T.CAUTION,
    T.CRITICAL,
)
S = PipelineStage


def _sr(
    label: str, stage: PipelineStage, tone: Tone, desc: str, terminal: bool = False
) -> EnumMeta:
    return EnumMeta(label, tone, desc, stage, terminal)


def _st(label: str, tone: Tone | None, terminal: bool = False, desc: str | None = None) -> EnumMeta:
    return EnumMeta(label, tone, desc, None, terminal)


def _l(label: str, tone: Tone | None = None, desc: str | None = None) -> EnumMeta:
    return EnumMeta(label, tone, desc)


ENUM_META: dict[type[StrEnum], dict[str, EnumMeta]] = {
    ServiceRequestStatus: {
        SR.IMPORTED: _sr(
            "Imported", S.IMPORTED, _I, "New from ITSM import; waiting for first mail"
        ),
        SR.IMPORTED_VIP: _sr(
            "Imported · VIP", S.IMPORTED, _I, "Requester on VIP list; no automatic mails"
        ),
        SR.HARDWARE_UNAVAILABLE: _sr(
            "Hardware unavailable", S.IMPORTED, _C, "On hardware hold; user informed once"
        ),
        SR.INVITED: _sr("Invited", S.INVITED, _O, "Invitation with booking link sent"),
        SR.CAMPUS_INFO_SENT: _sr(
            "Campus info sent", S.INVITED, _O, "Campus user told a technician will call"
        ),
        SR.REMINDER_1_SENT: _sr("Reminder 1 sent", S.INVITED, _O, "First reminder sent"),
        SR.REMINDER_2_SENT: _sr("Reminder 2 sent", S.INVITED, _O, "Second reminder sent"),
        SR.ESCALATED: _sr("Escalated", S.INVITED, _X, "Escalation mail sent"),
        SR.APPOINTMENT_BOOKED: _sr(
            "Appointment booked", S.BOOKED, _B, "Appointment exists; ready for staging"
        ),
        SR.PERSONALIZATION_IN_PROGRESS: _sr(
            "Personalizing", S.PERSONALIZING, _W, "Claimed by an editor"
        ),
        SR.PERSONALIZATION_DONE: _sr(
            "Personalization done", S.PERSONALIZING, _W, "Devices assigned; awaiting order closing"
        ),
        SR.READY_FOR_PICKING: _sr(
            "Ready for picking", S.READY, _W, "Order closed; assets prepared at goods-out"
        ),
        SR.SHIPPED: _sr(
            "Shipped", S.DELIVERY, _TR, "Parcel label created, hardware with parcel carrier"
        ),
        SR.PROVIDED: _sr(
            "Provided", S.DELIVERY, _TR, "Handed to technician (or closed without label)"
        ),
        SR.RESCHEDULE_NEEDED: _sr(
            "Reschedule needed",
            S.DELIVERY,
            _C,
            "Appointment cancelled/failed; waiting for a new one",
        ),
        SR.COMPLETED: _sr(
            "Deployed", S.DEPLOYED, _D, "Deployment done (or completed manually)", True
        ),
        SR.CANCELLED: _sr(
            "Cancelled", S.CLOSED, _I, "Cancelled (dispatcher, on site, or ITSM)", True
        ),
    },
    PipelineStage: {
        S.IMPORTED: _l("Imported", _I),
        S.INVITED: _l("Invited", _O),
        S.BOOKED: _l("Booked", _B),
        S.PERSONALIZING: _l("Personalizing", _W),
        S.READY: _l("Ready", _W),
        S.DELIVERY: _l("Shipped / provided", _TR),
        S.DEPLOYED: _l("Deployed", _D),
        S.CLOSED: _l("Closed", _I),
    },
    LeaseReturnStatus: {
        LR.IMPORTED: _st("Imported", _I, desc="Waiting for the info mail window"),
        LR.IMPORTED_VIP: _st("Imported · VIP", _I, desc="No automatic mails"),
        LR.INFO_SENT: _st("Info sent", _O, desc="Info mail sent"),
        LR.REMINDER_1_SENT: _st("Reminder 1 sent", _O),
        LR.REMINDER_2_SENT: _st("Reminder 2 sent", _O),
        LR.ESCALATED: _st("Escalated", _X),
        LR.REPLACEMENT_ORDERED: _st(
            "Replacement ordered", _D, desc="Linked to a replacement service request"
        ),
        LR.PROBABLY_CLOSED: _st(
            "Probably closed", _I, desc="Missing from the latest full lease import"
        ),
        LR.CLOSED: _st("Closed", _I, True, "Device collected, deactivated, or closed manually"),
    },
    AssetStatus: {
        AssetStatus.AVAILABLE: _st("Available", _I),
        AssetStatus.ASSIGNED: _st("Assigned to SR", _W),
        AssetStatus.PREPARED: _st("Prepared", _W),
        AssetStatus.PROVIDED: _st("Provided", _TR),
        AssetStatus.IN_USE: _st("In use", _D),
        AssetStatus.COLLECTED: _st("Return pending", _TR),
        AssetStatus.MISSING: _st("Missing", _X),
        AssetStatus.DEACTIVATED_RETURNED: _st("Deactivated · returned", _I, True),
        AssetStatus.DEACTIVATED_BOUGHT_OUT: _st("Deactivated · bought out", _I, True),
        AssetStatus.DEACTIVATED_DISPOSED: _st("Deactivated · disposed", _I, True),
    },
    LocationType: {
        LocationType.GOODS_IN: _l("Goods in"),
        LocationType.STAGING: _l("Staging"),
        LocationType.GOODS_OUT: _l("Goods out"),
        LocationType.RETURN: _l("Returns"),
        LocationType.IN_TRANSIT: _l(
            "In transit", desc="With technician, courier or parcel carrier"
        ),
        LocationType.IN_USE: _l("At user"),
        LocationType.OFF_FLEET: _l("Off fleet"),
    },
    DeliveryType: {
        DeliveryType.TECHNICIAN: _l("Technician delivery"),
        DeliveryType.PARCEL_TO_USER: _l("Parcel to user"),
        DeliveryType.PARCEL_TO_PICKUP_POINT: _l("Parcel to pickup point"),
    },
    ProcurementType: {
        ProcurementType.REPLACEMENT: _l("Replacement"),
        ProcurementType.NEW_HIRE: _l("New hire"),
        ProcurementType.ADDITIONAL_DEVICE: _l("Additional device"),
        ProcurementType.REMOVAL: _l("Removal only"),
    },
    ItemSlot: {
        ItemSlot.NEW_BASE: _l("New base device"),
        ItemSlot.NEW_PERIPHERAL_1: _l("New peripheral 1"),
        ItemSlot.NEW_PERIPHERAL_2: _l("New peripheral 2"),
        ItemSlot.OLD_BASE: _l("Old base device"),
        ItemSlot.OLD_PERIPHERAL_1: _l("Old peripheral 1"),
        ItemSlot.OLD_PERIPHERAL_2: _l("Old peripheral 2"),
    },
    OsReinstallTiming: {
        OsReinstallTiming.ON_PICKUP: _l(
            "On pickup", desc="Wipe and reinstall the old device when collected"
        ),
        OsReinstallTiming.ON_DELIVERY: _l(
            "On delivery", desc="Fresh OS install on the new device at the desk"
        ),
    },
    DeactivationReason: {
        DeactivationReason.RETURNED_TO_LESSOR: _l("Returned to lessor"),
        DeactivationReason.BOUGHT_OUT: _l("Bought out"),
        DeactivationReason.DISPOSED: _l("Disposed"),
    },
    Gender: {
        Gender.FEMALE: _l("Female"),
        Gender.MALE: _l("Male"),
        Gender.DIVERSE: _l("Diverse"),
        Gender.UNSPECIFIED: _l("Unspecified"),
    },
    Role: {
        Role.VIEWER: _l("Viewer", desc="Read-only access to operational screens"),
        Role.TECHNICIAN: _l("Technician", desc="Own tours, deployments and scan stations"),
        Role.DISPATCHER: _l("Dispatcher", desc="All operational work"),
        Role.ADMIN: _l("Admin", desc="Dispatcher plus master data, users and demo reset"),
    },
    SiteKind: {SiteKind.HUB: _l("Hub"), SiteKind.CAMPUS: _l("Campus")},
    RegionRuleMatchKind: {
        RegionRuleMatchKind.COST_CENTER: _l("Cost centre"),
        RegionRuleMatchKind.ZIP: _l("Zip"),
        RegionRuleMatchKind.ZIP_PREFIX: _l("Zip prefix"),
        RegionRuleMatchKind.CITY: _l("City"),
    },
    ArticleCategory: {
        ArticleCategory.LAPTOP: _l("Laptop"),
        ArticleCategory.DESKTOP: _l("Desktop"),
        ArticleCategory.TABLET: _l("Tablet"),
        ArticleCategory.DOCKING_STATION: _l("Docking station"),
        ArticleCategory.MONITOR: _l("Monitor"),
        ArticleCategory.HEADSET: _l("Headset"),
        ArticleCategory.KEYBOARD: _l("Keyboard"),
        ArticleCategory.MOUSE: _l("Mouse"),
        ArticleCategory.WEBCAM: _l("Webcam"),
        ArticleCategory.BAG: _l("Bag"),
        ArticleCategory.ACCESSORY: _l("Accessory"),
    },
    ImportType: {
        ImportType.LEASE_RETURNS: _l("Lease returns"),
        ImportType.SERVICE_REQUESTS: _l("Service requests"),
        ImportType.CONTACTS: _l("Contacts"),
        ImportType.ASSETS_NEW: _l("New assets"),
        ImportType.ASSET_STATUS_SYNC: _l("Asset status sync"),
    },
    ImportJobStatus: {
        ImportJobStatus.QUEUED: _st("Queued", _I),
        ImportJobStatus.PROCESSING: _st("Importing", _O),
        ImportJobStatus.COMPLETED: _st("Imported", _D, True),
        ImportJobStatus.COMPLETED_WITH_ERRORS: _st("Imported with issues", _C, True),
        ImportJobStatus.FAILED: _st("Failed", _X, True),
        ImportJobStatus.CANCELLED: _st("Cancelled", _I, True),
    },
    InventoryStatus: {
        InventoryStatus.OPEN: _st("Counting", _B),
        InventoryStatus.CLOSED: _st("Closed", _I, True),
        InventoryStatus.CANCELLED: _st("Cancelled", _I, True),
    },
    InventoryScanResult: {
        InventoryScanResult.MATCHED: _l("Matched", _D),
        InventoryScanResult.WRONG_LOCATION: _l("Wrong location", _C),
        InventoryScanResult.UNKNOWN: _l("Unknown", _X),
    },
    TourStatus: {
        TourStatus.DRAFT: _st("No technician", _C),
        TourStatus.PLANNED: _st("Assigned", _B),
        TourStatus.IN_PROGRESS: _st("On the road", _TR),
        TourStatus.COMPLETED: _st("Done", _D, True),
        TourStatus.CANCELLED: _st("Cancelled", _I, True),
    },
    TourStopOutcome: {
        TourStopOutcome.PENDING: _l("Pending", _I),
        TourStopOutcome.COMPLETED: _l("Done", _D),
        TourStopOutcome.RESCHEDULED: _l("Rescheduled", _C),
        TourStopOutcome.CANCELLED: _l("Cancelled", _X),
    },
    CallOffOrderStatus: {
        CallOffOrderStatus.DRAFT: _st("Draft", _I),
        CallOffOrderStatus.SENT: _st("Sent to Initech Logistics", _O, True),
        CallOffOrderStatus.CANCELLED: _st("Cancelled", _I, True),
    },
    JobStatus: {
        JobStatus.QUEUED: _st("Queued", _I),
        JobStatus.RUNNING: _st("Running", _O),
        JobStatus.SUCCEEDED: _st("Succeeded", _D, True),
        JobStatus.FAILED: _st("Failed", _X, True),
        JobStatus.CANCELLED: _st("Cancelled", _I, True),
    },
    JobKind: {
        JobKind.IMPORT: _l("Import"),
        JobKind.APPOINTMENTS_SYNC: _l("Appointment sync"),
        JobKind.STOCK_RECALCULATE: _l("Stock recalculation"),
        JobKind.COMMS_DISPATCH: _l("Communication dispatch"),
        JobKind.REGIONS_REAPPLY: _l("Region re-resolution"),
        JobKind.DEMO_RESET: _l("Demo reset"),
    },
    DeviationCategory: {
        DeviationCategory.PROVIDER_RESCHEDULE: _l("Provider reschedule"),
        DeviationCategory.CUSTOMER_RESCHEDULE: _l("Customer reschedule"),
        DeviationCategory.REFUSAL: _l("Refusal"),
        DeviationCategory.EXTRA_EFFORT: _l("Extra effort"),
        DeviationCategory.CANCELLATION: _l("Cancellation"),
        DeviationCategory.SITE_ISSUE: _l("Site issue"),
    },
    DeviationOutcome: {
        DeviationOutcome.COMPLETE: _l("Complete", _D),
        DeviationOutcome.RESCHEDULE: _l("Reschedule", _C),
        DeviationOutcome.CANCEL: _l("Cancel", _X),
    },
    MailStatus: {
        MailStatus.QUEUED: _l("Queued", _I),
        MailStatus.SENT: _l("Sent", _D),
        MailStatus.FAILED: _l("Failed", _X),
    },
    AppointmentStatus: {
        AppointmentStatus.BOOKED: _l("Booked", _B),
        AppointmentStatus.CANCELLED: _l("Cancelled", _C),
    },
    ScanVerdict: {
        ScanVerdict.OK: _l("OK", _D),
        ScanVerdict.WARNING: _l("Warning", _C),
        ScanVerdict.ERROR: _l("Error", _X),
    },
    AssetSource: {
        AssetSource.IMPORT_NEW: _l("Import (new assets)"),
        AssetSource.IMPORT_STATUS_SYNC: _l("Import (status sync)"),
        AssetSource.TECHNICIAN_SCAN: _l("Technician scan"),
        AssetSource.MANUAL: _l("Manual"),
        AssetSource.SEED: _l("Seed"),
    },
    Readiness: {
        Readiness.READY: _l("Ready", _TR),
        Readiness.TO_PICK: _l("To pick", _W),
        Readiness.IN_STAGING: _l("In staging", _W),
        Readiness.NOT_STARTED: _l("Not started", _C),
    },
    AttentionFlag: {
        AttentionFlag.REGION_UNRESOLVED: _l(
            "Region unresolved", _C, "No region rule matched the address"
        ),
        AttentionFlag.CONTACT_EMAIL_MISSING: _l(
            "Email missing", _C, "Contact has no email address"
        ),
        AttentionFlag.UNKNOWN_ARTICLE: _l(
            "Unknown device type", _C, "Ordered type is not in the catalogue"
        ),
        AttentionFlag.APPOINTMENT_SOON_NOT_READY: _l(
            "Appointment soon, not ready",
            _C,
            "Appointment by the next working day but staging unfinished",
        ),
        AttentionFlag.APPOINTMENT_OVERDUE: _l(
            "Appointment overdue", _X, "Appointment ended more than 24 h ago"
        ),
        AttentionFlag.ESCALATED_STALE: _l("Escalation stale", _X, "Escalated at least 7 days ago"),
        AttentionFlag.ITSM_CLOSED: _l("Closed in ITSM", _C, "ITSM state is Closed Complete"),
        AttentionFlag.HOLD_GROUP_LEFT: _l(
            "Left hold group", _C, "On hold but no longer in the hold assignment group"
        ),
        AttentionFlag.RETIREMENT_PASSED: _l(
            "Retirement passed", _C, "Lease end date is in the past"
        ),
        AttentionFlag.NOT_COLLECTED: _l(
            "Not collected", _C, "Replacement deployed but old device not collected"
        ),
    },
    HoldSource: {HoldSource.IMPORT: _l("ITSM hold group"), HoldSource.MANUAL: _l("Set manually")},
    Trigger: {
        Trigger.USER: _l("User"),
        Trigger.JOB: _l("Scheduled job"),
        Trigger.IMPORT: _l("Import"),
        Trigger.SYNC: _l("Booking sync"),
        Trigger.SYSTEM: _l("System"),
    },
    AssetAction: {
        AssetAction.RECEIVE: _l("Receive"),
        AssetAction.PLACE: _l("Place on shelf"),
        AssetAction.ASSIGN: _l("Assign to SR"),
        AssetAction.UNASSIGN: _l("Unassign"),
        AssetAction.PREPARE: _l("Prepare"),
        AssetAction.PROVIDE: _l("Provide"),
        AssetAction.RETURN_TO_HUB: _l("Return to hub"),
        AssetAction.RELEASE: _l("Release"),
        AssetAction.DEPLOY: _l("Deploy"),
        AssetAction.REGISTER_UNKNOWN: _l("Register unknown device"),
        AssetAction.COLLECT: _l("Collect"),
        AssetAction.SYNC_IN_USE: _l("Sync: in use"),
        AssetAction.SYNC_COLLECTED: _l("Sync: collected"),
        AssetAction.MARK_MISSING: _l("Mark missing"),
        AssetAction.MARK_FOUND: _l("Mark found"),
        AssetAction.INVENTORY_RELOCATE: _l("Inventory relocation"),
        AssetAction.DEACTIVATE: _l("Deactivate"),
        AssetAction.CORRECT: _l("Correct status/location"),
    },
}

#: ``GET /meta/enums`` response keys, in contract order (API.md §3.3).
ENUM_API_KEYS: dict[str, type[StrEnum]] = {
    "serviceRequestStatus": ServiceRequestStatus,
    "pipelineStage": PipelineStage,
    "leaseReturnStatus": LeaseReturnStatus,
    "assetStatus": AssetStatus,
    "locationType": LocationType,
    "deliveryType": DeliveryType,
    "procurementType": ProcurementType,
    "itemSlot": ItemSlot,
    "osReinstallTiming": OsReinstallTiming,
    "deactivationReason": DeactivationReason,
    "gender": Gender,
    "role": Role,
    "siteKind": SiteKind,
    "regionRuleMatchKind": RegionRuleMatchKind,
    "articleCategory": ArticleCategory,
    "importType": ImportType,
    "importJobStatus": ImportJobStatus,
    "inventoryStatus": InventoryStatus,
    "inventoryScanResult": InventoryScanResult,
    "tourStatus": TourStatus,
    "tourStopOutcome": TourStopOutcome,
    "callOffOrderStatus": CallOffOrderStatus,
    "jobStatus": JobStatus,
    "jobKind": JobKind,
    "deviationCategory": DeviationCategory,
    "deviationOutcome": DeviationOutcome,
    "mailStatus": MailStatus,
    "appointmentStatus": AppointmentStatus,
    "attentionFlag": AttentionFlag,
    "trigger": Trigger,
    "assetAction": AssetAction,
    "readiness": Readiness,
    # SPEC_RESOLUTIONS #17
    "scanVerdict": ScanVerdict,
    "assetSource": AssetSource,
}


def label(value: StrEnum | str, enum_cls: type[StrEnum] | None = None) -> str:
    cls = cast(type[StrEnum], enum_cls or type(value))
    meta = ENUM_META.get(cls, {}).get(str(value))
    return meta.label if meta else str(value)


def tone(value: StrEnum | str, enum_cls: type[StrEnum] | None = None) -> Tone | None:
    cls = cast(type[StrEnum], enum_cls or type(value))
    meta = ENUM_META.get(cls, {}).get(str(value))
    return meta.tone if meta else None


def stage_of(status: ServiceRequestStatus | str) -> PipelineStage:
    meta = ENUM_META[ServiceRequestStatus][str(status)]
    assert meta.stage is not None
    return meta.stage
