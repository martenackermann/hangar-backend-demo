"""The five import formats (SPEC §7.3–§7.7) and type detection precedence (§7.2)."""

from __future__ import annotations

from app.core.enums import ImportType

from .spec import Column, Kind, OptionSpec, PackedKey, TypeSpec

ASSET_TAG = r"^[A-Z0-9-]{4,20}$"
SERIAL = r"^[A-Z0-9-]{4,40}$"
USER_ID = r"^[A-Z0-9]{3,16}$"
COST_CENTER = r"^[A-Z0-9-]{2,20}$"
SR_NUMBER = r"^SR[0-9]{7}$"
TASK_NUMBER = r"^TASK[0-9]{7}$"

ITSM_STATES = ("New", "Open", "Work in Progress", "On Hold", "Closed Complete", "Cancelled")
PROCUREMENT_TYPES = {
    "replacement": "replacement",
    "new hire": "new_hire",
    "additional device": "additional_device",
    "removal": "removal",
}

_GENDER = Column(
    "gender",
    kind=Kind.GENDER,
    format="female | male | diverse | unspecified (or f | m | d | u)",
    description="Contact gender (salutation only). Unknown values are ignored with a warning.",
    example="female",
)
_FIRST = Column(
    "first_name", max_len=80, format="text ≤ 80", description="Contact first name", example="Mia"
)
_LAST = Column(
    "last_name",
    required=True,
    max_len=80,
    format="text ≤ 80",
    description="Contact last name",
    example="Turner",
)
_EMAIL = Column(
    "email",
    kind=Kind.EMAIL,
    max_len=254,
    format="email",
    description="Contact email (lower-cased). Missing → warning email_missing.",
    example="mia.turner@example.com",
)
_COST_CENTER = Column(
    "cost_center",
    kind=Kind.KEY,
    pattern=COST_CENTER,
    max_len=20,
    format="^[A-Z0-9-]{2,20}$",
    description="Cost centre (upper-cased); used for region and campus rules",
    example="CC-4410",
)
_USER_ID = Column(
    "user_id",
    required=True,
    kind=Kind.KEY,
    pattern=USER_ID,
    max_len=16,
    format="^[A-Z0-9]{3,16}$",
    description="ITSM user id of the employee (contact upsert key)",
    example="E104233",
)

LEASE_RETURNS = TypeSpec(
    type=ImportType.LEASE_RETURNS,
    label="Lease returns",
    description="Leased devices approaching their retirement date (ITSM asset export).",
    signature=("retirement_date",),
    key="asset_tag",
    columns=(
        Column(
            "asset_tag",
            required=True,
            kind=Kind.KEY,
            pattern=ASSET_TAG,
            max_len=20,
            format="^[A-Z0-9-]{4,20}$",
            description="Asset tag of the leased device (upsert key)",
            example="GX101877",
        ),
        Column(
            "serial_number",
            kind=Kind.KEY,
            pattern=SERIAL,
            max_len=40,
            format="^[A-Z0-9-]{4,40}$",
            description="Serial number",
            example="KD7F3K21Q9",
        ),
        Column(
            "model_category",
            max_len=80,
            format="text ≤ 80",
            description="Model category",
            example="Notebook",
        ),
        Column(
            "checked_in_date",
            kind=Kind.DATE_OR_DATETIME,
            format="YYYY-MM-DD or datetime",
            description="Goods receipt of the device",
            example="2023-04-11",
        ),
        Column(
            "retirement_date",
            required=True,
            kind=Kind.DATE,
            format="YYYY-MM-DD",
            description="Lease end",
            example="2026-12-01",
        ),
        Column(
            "model",
            max_len=120,
            format="text ≤ 120",
            description="Device model",
            example="Aero 14 G2 Laptop",
        ),
        _USER_ID,
        Column(
            "install_status",
            max_len=40,
            format="text ≤ 40",
            description="ITSM install status",
            example="In use",
        ),
        Column(
            "ci_name", max_len=80, format="text ≤ 80", description="CI name", example="NB-40211"
        ),
        _GENDER,
        _FIRST,
        _LAST,
        _EMAIL,
        _COST_CENTER,
        Column(
            "escalation_email",
            kind=Kind.OPTIONAL_EMAIL,
            max_len=254,
            format="email",
            description="Extra escalation recipient (invalid → warning, ignored)",
            example=None,
        ),
    ),
    options=(
        OptionSpec(
            "markMissingAsProbablyClosed",
            False,
            "Open lease returns missing from this file are moved to 'Probably closed'.",
        ),
        OptionSpec("dryRun", False, "Validate only; nothing is changed."),
    ),
)

ORDER_DETAIL_KEYS: tuple[PackedKey, ...] = (
    PackedKey(
        "Procurement type",
        "procurement_type",
        required=True,
        kind=Kind.PROCUREMENT,
        description="Replacement, New hire, Additional device or Removal",
    ),
    PackedKey("Scope of delivery", "scope_of_delivery", max_len=200, description="Free text"),
    PackedKey("Client component", "client_component", max_len=120, description="Free text"),
    PackedKey(
        "Old base device",
        "old_base",
        kind=Kind.KEY,
        pattern=ASSET_TAG,
        max_len=20,
        description="Asset tag of the old base device to collect",
    ),
    PackedKey(
        "Old peripheral 1",
        "old_peripheral_1",
        kind=Kind.KEY,
        pattern=ASSET_TAG,
        max_len=20,
        description="Asset tag of an old peripheral",
    ),
    PackedKey(
        "Old peripheral 2",
        "old_peripheral_2",
        kind=Kind.KEY,
        pattern=ASSET_TAG,
        max_len=20,
        description="Asset tag of an old peripheral",
    ),
    PackedKey(
        "New base device type",
        "new_base",
        max_len=120,
        required_unless_removal=True,
        description="Ordered device type (article internal description); required unless Removal",
    ),
    PackedKey("New peripheral 1 type", "new_peripheral_1", max_len=120, description="Device type"),
    PackedKey("New peripheral 2 type", "new_peripheral_2", max_len=120, description="Device type"),
    PackedKey(
        "Software without approval",
        "requested_software",
        kind=Kind.SOFTWARE,
        description="';'-separated software list",
    ),
    PackedKey("Note", "note", max_len=2000, description="Note for the rollout team"),
    PackedKey(
        "On-site contact phone",
        "onsite_contact_phone",
        kind=Kind.PHONE,
        description="Phone number (invalid → warning, dropped)",
    ),
)
DELIVERY_ADDRESS_KEYS: tuple[PackedKey, ...] = (
    PackedKey("Room", "room", max_len=40, description="Room"),
    PackedKey("Building", "building", max_len=40, description="Building"),
    PackedKey("Street", "street", required=True, max_len=160, description="Street and number"),
    PackedKey("Zip", "zip", required=True, kind=Kind.ZIP, description="4–5 digits (padded to 5)"),
    PackedKey("City", "city", required=True, max_len=80, description="City"),
    PackedKey("Country", "country", max_len=60, description="Default Freeland"),
)

SERVICE_REQUESTS = TypeSpec(
    type=ImportType.SERVICE_REQUESTS,
    label="Service requests",
    description="Hardware orders from the ITSM export, with packed order details and address.",
    signature=("order_details",),
    key="sr_number",
    columns=(
        Column(
            "sr_number",
            required=True,
            kind=Kind.KEY,
            pattern=SR_NUMBER,
            max_len=10,
            format="^SR[0-9]{7}$",
            description="Service request number (upsert key)",
            example="SR0012345",
        ),
        Column(
            "task_number",
            kind=Kind.KEY,
            pattern=TASK_NUMBER,
            max_len=12,
            format="^TASK[0-9]{7}$",
            description="ITSM task number",
            example="TASK0045678",
        ),
        Column(
            "itsm_state",
            kind=Kind.ITSM_STATE,
            max_len=40,
            format=" | ".join(ITSM_STATES),
            description="ITSM state; 'Cancelled' cancels the request",
            example="Open",
        ),
        Column(
            "assignment_group",
            max_len=120,
            format="text ≤ 120",
            description="ITSM assignment group; the hold group puts the request on hardware hold",
            example="Workplace Rollout",
        ),
        Column(
            "opened_at",
            kind=Kind.DATETIME,
            format="YYYY-MM-DDTHH:MM[:SS][Z|±HH:MM]",
            description="Opened in ITSM",
            example="2026-10-12T08:14:00Z",
        ),
        _USER_ID,
        Column(
            "catalog_item",
            max_len=160,
            format="text ≤ 160",
            description="ITSM catalog item",
            example="Workplace hardware replacement",
        ),
        Column(
            "short_description",
            max_len=2000,
            format="text ≤ 2000",
            description="ITSM short description",
            example="Replace laptop and dock",
        ),
        Column(
            "order_details",
            required=True,
            kind=Kind.PACKED,
            format="[Key: value],[Key: value],…",
            description="Packed order details (see keys)",
            example="[Procurement type: Replacement],[New base device type: Laptop Standard 14]",
        ),
        Column(
            "delivery_address",
            required=True,
            kind=Kind.PACKED,
            format="[Key: value],[Key: value],…",
            description="Packed delivery address (see keys)",
            example="[Street: 18 Harbour Road],[Zip: 50110],[City: Westbay]",
        ),
        _GENDER,
        _FIRST,
        _LAST,
        _EMAIL,
        _COST_CENTER,
    ),
    packed={"order_details": ORDER_DETAIL_KEYS, "delivery_address": DELIVERY_ADDRESS_KEYS},
    options=(OptionSpec("dryRun", False, "Validate only; nothing is changed."),),
)

CONTACTS = TypeSpec(
    type=ImportType.CONTACTS,
    label="Contacts",
    description="Employee master data (ITSM user export).",
    signature=("user_id", "last_name"),
    key="user_id",
    columns=(
        _USER_ID,
        _GENDER,
        _FIRST,
        _LAST,
        _EMAIL,
        Column(
            "phone", max_len=40, format="text ≤ 40", description="Phone", example="+00 555 0142"
        ),
        Column("mobile", max_len=40, format="text ≤ 40", description="Mobile", example=None),
        Column(
            "department",
            max_len=120,
            format="text ≤ 120",
            description="Department",
            example="Finance",
        ),
        Column(
            "street",
            max_len=160,
            format="text ≤ 160",
            description="Street",
            example="18 Harbour Road",
        ),
        Column(
            "zip",
            kind=Kind.ZIP,
            format="4–5 digits",
            description="Zip (padded to 5 digits)",
            example="50110",
        ),
        Column("city", max_len=80, format="text ≤ 80", description="City", example="Westbay"),
        Column("building", max_len=40, format="text ≤ 40", description="Building", example="B2"),
        Column("room", max_len=40, format="text ≤ 40", description="Room", example="3.14"),
    ),
    options=(OptionSpec("dryRun", False, "Validate only; nothing is changed."),),
)

_MODEL = Column(
    "model",
    required=True,
    max_len=80,
    format="article model number",
    description="Model number; unknown models → warning unknown_model",
    example="KD-AERO14-G3",
)

ASSETS_NEW = TypeSpec(
    type=ImportType.ASSETS_NEW,
    label="New assets",
    description="Goods receipt of new devices from Initech Logistics (received into a hub).",
    signature=("serial_number",),
    key="serial_number",
    columns=(
        Column(
            "order_number",
            max_len=40,
            format="text ≤ 40",
            description="Partner delivery/order number",
            example="PO-2610-311",
        ),
        Column(
            "serial_number",
            required=True,
            kind=Kind.KEY,
            pattern=SERIAL,
            max_len=40,
            format="^[A-Z0-9-]{4,40}$",
            description="Serial number (upsert key)",
            example="KD7F3K21Q9",
        ),
        Column(
            "asset_tag",
            kind=Kind.KEY,
            pattern=ASSET_TAG,
            max_len=20,
            format="^[A-Z0-9-]{4,20}$",
            description="Asset tag (unique)",
            example="GX260001",
        ),
        Column(
            "external_asset_id",
            max_len=40,
            format="text ≤ 40",
            description="ITSM asset id",
            example="AST482211",
        ),
        Column("ci_name", max_len=80, format="text ≤ 80", description="CI name", example=None),
        _MODEL,
        Column(
            "mac_address",
            kind=Kind.MAC,
            format="12 hex digits, ':' or '-' optional",
            description="MAC address (invalid → warning, dropped)",
            example="02:00:5E:10:20:30",
        ),
        Column(
            "checked_in_date",
            kind=Kind.DATE_OR_DATETIME,
            format="YYYY-MM-DD or datetime",
            description="Goods receipt (default: import time); FIFO key",
            example="2026-10-12",
        ),
    ),
    options=(
        OptionSpec(
            "targetLocationId",
            True,
            "Hub location (goods in or staging) the devices are received into.",
        ),
        OptionSpec("dryRun", False, "Validate only; nothing is changed."),
    ),
)

INSTALL_STATES = ("In use", "In transit", "In stock")
OLD_DEVICE_STOCKROOM = "Old device stockroom"

ASSET_STATUS_SYNC = TypeSpec(
    type=ImportType.ASSET_STATUS_SYNC,
    label="Asset status sync",
    description="ITSM install states of fleet devices (in use, in transit, returned).",
    signature=("install_state",),
    key="serial_number",
    columns=(
        Column(
            "serial_number",
            required=True,
            kind=Kind.KEY,
            pattern=SERIAL,
            max_len=40,
            format="^[A-Z0-9-]{4,40}$",
            description="Serial number (upsert key)",
            example="KD7F3K21Q9",
        ),
        Column(
            "install_state",
            required=True,
            max_len=40,
            format="In use | In transit | In stock",
            description="ITSM install state; other values are skipped (unmapped_state)",
            example="In use",
        ),
        Column(
            "stockroom",
            max_len=80,
            format="text",
            description="'Old device stockroom' marks returned devices",
            example="Old device stockroom",
        ),
        Column(
            "sr_number",
            kind=Kind.KEY,
            pattern=SR_NUMBER,
            max_len=10,
            format="^SR[0-9]{7}$",
            description="Service request the device belongs to",
            example=None,
        ),
        Column(
            "assigned_user_id",
            kind=Kind.KEY,
            pattern=USER_ID,
            max_len=16,
            format="^[A-Z0-9]{3,16}$",
            description="User holding the device (In use)",
            example="E104233",
        ),
        Column(
            "asset_tag",
            kind=Kind.KEY,
            pattern=ASSET_TAG,
            max_len=20,
            format="^[A-Z0-9-]{4,20}$",
            description="Asset tag",
            example="GX101877",
        ),
        Column(
            "model",
            max_len=80,
            format="article model number",
            description="Model number",
            example="KD-AERO14-G2",
        ),
        Column("order_number", max_len=40, format="text ≤ 40", description="Order number"),
        Column("external_asset_id", max_len=40, format="text ≤ 40", description="ITSM asset id"),
        Column("ci_name", max_len=80, format="text ≤ 80", description="CI name"),
        Column(
            "mac_address",
            kind=Kind.MAC,
            format="12 hex digits",
            description="MAC address (invalid → warning, dropped)",
        ),
        Column(
            "checked_in_date",
            kind=Kind.DATE_OR_DATETIME,
            format="YYYY-MM-DD or datetime",
            description="Goods receipt",
        ),
    ),
    options=(OptionSpec("dryRun", False, "Validate only; nothing is changed."),),
)

SPECS: dict[ImportType, TypeSpec] = {
    spec.type: spec
    for spec in (SERVICE_REQUESTS, LEASE_RETURNS, ASSET_STATUS_SYNC, ASSETS_NEW, CONTACTS)
}

#: SPEC §7.2 — first match wins.
DETECTION_ORDER: tuple[ImportType, ...] = (
    ImportType.SERVICE_REQUESTS,
    ImportType.LEASE_RETURNS,
    ImportType.ASSET_STATUS_SYNC,
    ImportType.ASSETS_NEW,
    ImportType.CONTACTS,
)


def detect_type(normalized_headers: list[str] | tuple[str, ...]) -> ImportType | None:
    present = set(normalized_headers)
    for import_type in DETECTION_ORDER:
        if all(h in present for h in SPECS[import_type].signature):
            return import_type
    return None
