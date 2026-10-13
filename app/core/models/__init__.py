"""Core tables (SPEC §5.1). Importing this package registers them on ``Base.metadata``."""

from app.core.models.assets import Asset, AssetMovement
from app.core.models.leases import LeaseReturn, LeaseReturnComment
from app.core.models.master import (
    Article,
    ArticleSiteSetting,
    DeviationCode,
    Region,
    RegionRule,
    Site,
    StockLocation,
)
from app.core.models.people import Contact, SavedView, User, VipEntry
from app.core.models.requests import ServiceRequest, ServiceRequestItem
from app.core.models.system import (
    BOOKING_SANDBOX_SEQ,
    FAKE_CARRIER_SEQ,
    BackgroundJob,
    BookingSandboxAppointment,
    StatusTransition,
    StoredFile,
)

#: Tables owned by core, in dependency order (used by demo truncation and tests).
CORE_TABLES: tuple[str, ...] = (
    "users",
    "saved_views",
    "contacts",
    "vip_entries",
    "sites",
    "regions",
    "region_rules",
    "stock_locations",
    "articles",
    "article_site_settings",
    "assets",
    "asset_movements",
    "service_requests",
    "service_request_items",
    "lease_returns",
    "lease_return_comments",
    "status_transitions",
    "deviation_codes",
    "stored_files",
    "background_jobs",
    "booking_sandbox_appointments",
)

__all__ = [
    "BOOKING_SANDBOX_SEQ",
    "CORE_TABLES",
    "FAKE_CARRIER_SEQ",
    "Article",
    "ArticleSiteSetting",
    "Asset",
    "AssetMovement",
    "BackgroundJob",
    "BookingSandboxAppointment",
    "Contact",
    "DeviationCode",
    "LeaseReturn",
    "LeaseReturnComment",
    "Region",
    "RegionRule",
    "SavedView",
    "ServiceRequest",
    "ServiceRequestItem",
    "Site",
    "StatusTransition",
    "StockLocation",
    "StoredFile",
    "User",
    "VipEntry",
]
