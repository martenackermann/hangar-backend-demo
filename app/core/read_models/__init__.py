"""Shared read models (SPEC_RESOLUTIONS #1): response shapes built from core tables.

Slices call these builders and never re-implement the shapes:
``build_sr_list_items`` / ``build_sr_detail``, ``build_lease_list_items`` /
``build_lease_detail``, ``build_asset_list_items`` / ``build_asset_detail``,
``build_contact_summaries`` / ``build_contact_detail``, attention flags + SQL predicates.
"""

from app.core.read_models.assets import (
    AssetDetail,
    AssetListItem,
    AssetMovementOut,
    build_asset_detail,
    build_asset_list_items,
    build_movement,
)
from app.core.read_models.attention import (
    LEASE_FLAGS,
    SR_FLAGS,
    lease_attention,
    lease_attention_clause,
    lease_needs_attention_clause,
    sr_attention,
    sr_attention_clause,
    sr_needs_attention_clause,
)
from app.core.read_models.contacts import (
    ContactDetail,
    ContactListItem,
    ContactSummary,
    build_contact_detail,
    build_contact_summaries,
)
from app.core.read_models.lease_returns import (
    LeaseReturnDetail,
    LeaseReturnListItem,
    build_lease_detail,
    build_lease_list_items,
)
from app.core.read_models.refs import (
    article_ref,
    asset_summary,
    contact_ref,
    deviation_code_ref,
    location_ref,
    region_ref,
    site_ref,
    sr_ref,
    user_ref,
)
from app.core.read_models.service_requests import (
    ServiceRequestDetail,
    ServiceRequestItemOut,
    ServiceRequestListItem,
    booking_link,
    build_item,
    build_sr_detail,
    build_sr_list_item,
    build_sr_list_items,
    vip_clause,
)

__all__ = [
    "LEASE_FLAGS",
    "SR_FLAGS",
    "AssetDetail",
    "AssetListItem",
    "AssetMovementOut",
    "ContactDetail",
    "ContactListItem",
    "ContactSummary",
    "LeaseReturnDetail",
    "LeaseReturnListItem",
    "ServiceRequestDetail",
    "ServiceRequestItemOut",
    "ServiceRequestListItem",
    "article_ref",
    "asset_summary",
    "booking_link",
    "build_asset_detail",
    "build_asset_list_items",
    "build_contact_detail",
    "build_contact_summaries",
    "build_item",
    "build_lease_detail",
    "build_lease_list_items",
    "build_movement",
    "build_sr_detail",
    "build_sr_list_item",
    "build_sr_list_items",
    "contact_ref",
    "deviation_code_ref",
    "lease_attention",
    "lease_attention_clause",
    "lease_needs_attention_clause",
    "location_ref",
    "region_ref",
    "site_ref",
    "sr_attention",
    "sr_attention_clause",
    "sr_needs_attention_clause",
    "sr_ref",
    "user_ref",
    "vip_clause",
]
