"""Reference data of the seed story (SPEC §11.1, §6.13, §3.3) as plain tuples.

Also used by migration ``0001_core`` (deviation codes are rows of the migration).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import (
    ArticleCategory,
    DeliveryType,
    DeviationCategory,
    DeviationOutcome,
    ProcurementType,
    RegionRuleMatchKind,
    Role,
    SiteKind,
)

DEMO_PASSWORD = "demo1234"  # noqa: S105 - documented demo-only persona password
COUNTRY = "Freeland"


@dataclass(frozen=True, slots=True)
class SiteDef:
    code: str
    name: str
    kind: SiteKind
    street: str
    zip: str
    city: str
    parent: str | None = None


SITES: tuple[SiteDef, ...] = (
    SiteDef("RIV", "Riverton Hub", SiteKind.HUB, "12 Mill Road", "10010", "Riverton"),
    SiteDef("HIL", "Hillford Hub", SiteKind.HUB, "3 Station Lane", "20020", "Hillford"),
    SiteDef("EAS", "Eastgate Hub", SiteKind.HUB, "88 Dock Road", "30030", "Eastgate"),
    SiteDef("CMP", "Campus", SiteKind.CAMPUS, "1 Campus Way", "10500", "Riverton", parent="RIV"),
)

HUB_LOCATION_TYPES = ("goods_in", "staging", "goods_out", "return")
LOCATION_LABELS = {
    "goods_in": "Goods in",
    "staging": "Staging",
    "goods_out": "Goods out",
    "return": "Returns",
    "in_transit": "In transit",
    "in_use": "At user",
    "off_fleet": "Off fleet",
}


@dataclass(frozen=True, slots=True)
class RegionDef:
    code: str
    name: str
    hub: str
    delivery: DeliveryType
    zip_prefix: str
    pickup: tuple[str, str, str, str] | None = None  # name, street, zip, city


REGIONS: tuple[RegionDef, ...] = (
    RegionDef("riverton", "Riverton", "RIV", DeliveryType.TECHNICIAN, "10"),
    RegionDef("hillford", "Hillford", "HIL", DeliveryType.TECHNICIAN, "20"),
    RegionDef("eastgate", "Eastgate", "EAS", DeliveryType.TECHNICIAN, "30"),
    RegionDef(
        "northport",
        "Northport",
        "EAS",
        DeliveryType.PARCEL_TO_PICKUP_POINT,
        "40",
        ("Northport Office", "4 Quay Street", "40100", "Northport"),
    ),
    RegionDef("westbay", "Westbay", "EAS", DeliveryType.PARCEL_TO_USER, "50"),
    RegionDef(
        "southmere",
        "Southmere",
        "EAS",
        DeliveryType.PARCEL_TO_PICKUP_POINT,
        "60",
        ("Southmere Office", "21 Market Row", "60200", "Southmere"),
    ),
    RegionDef("lakeside", "Lakeside", "EAS", DeliveryType.PARCEL_TO_USER, "70"),
    RegionDef(
        "highmoor",
        "Highmoor",
        "EAS",
        DeliveryType.PARCEL_TO_PICKUP_POINT,
        "80",
        ("Highmoor Office", "7 Ridge Avenue", "80300", "Highmoor"),
    ),
    RegionDef("oakridge", "Oakridge", "EAS", DeliveryType.PARCEL_TO_USER, "90"),
)


@dataclass(frozen=True, slots=True)
class RuleDef:
    region: str
    kind: RegionRuleMatchKind
    pattern: str
    procurement: ProcurementType | None = None
    delivery: DeliveryType | None = None
    campus: bool = False
    priority: int = 0
    note: str | None = None


REGION_RULES: tuple[RuleDef, ...] = (
    *(RuleDef(r.code, RegionRuleMatchKind.ZIP_PREFIX, r.zip_prefix) for r in REGIONS),
    *(RuleDef(r.code, RegionRuleMatchKind.CITY, r.name.casefold()) for r in REGIONS),
    RuleDef("riverton", RegionRuleMatchKind.ZIP, "10500", campus=True, note="Campus address"),
    RuleDef(
        "riverton",
        RegionRuleMatchKind.COST_CENTER,
        "CC-9100",
        campus=True,
        note="Campus cost centre",
    ),
    RuleDef(
        "westbay",
        RegionRuleMatchKind.ZIP_PREFIX,
        "50",
        ProcurementType.REMOVAL,
        DeliveryType.TECHNICIAN,
        priority=10,
        note="Removals are collected by a technician",
    ),
)

K, L, Q, AP, PW = "Kestrel Devices", "Lumora", "Quillon Audio", "Arden Peripherals", "Packwell"
C = ArticleCategory

#: number, category, name, model number, device type, active, kg, manufacturer
ARTICLES: tuple[tuple[str, ArticleCategory, str, str, str, bool, str, str], ...] = (
    (
        "A-10001",
        C.LAPTOP,
        "Aero 14 G3 Laptop",
        "KD-AERO14-G3",
        "Laptop Standard 14",
        True,
        "1.6",
        K,
    ),
    (
        "A-10002",
        C.LAPTOP,
        "Aero 16 G3 Laptop",
        "KD-AERO16-G3",
        "Laptop Performance 16",
        True,
        "2.1",
        K,
    ),
    (
        "A-10003",
        C.LAPTOP,
        "Aero 13 Flip Laptop",
        "KD-AERO13-FLIP",
        "Laptop Convertible 13",
        True,
        "1.3",
        K,
    ),
    (
        "A-10004",
        C.LAPTOP,
        "Strato X1 Mobile Workstation",
        "KD-STRATO-X1",
        "Laptop Workstation 15",
        True,
        "2.4",
        K,
    ),
    (
        "A-10005",
        C.LAPTOP,
        "Aero 14 G2 Laptop",
        "KD-AERO14-G2",
        "Laptop Standard 14 (previous)",
        False,
        "1.7",
        K,
    ),
    ("A-10006", C.DESKTOP, "Cube Mini D5", "KD-CUBE-D5", "Desktop Compact", True, "1.2", K),
    ("A-10007", C.TABLET, "Slate 11 Tablet", "KD-SLATE-11", "Tablet 11", True, "0.6", K),
    (
        "A-10101",
        C.DOCKING_STATION,
        "Dock U4",
        "KD-DOCK-U4",
        "Docking Station USB-C",
        True,
        "0.8",
        K,
    ),
    (
        "A-10102",
        C.DOCKING_STATION,
        "Dock T5 Pro",
        "KD-DOCK-T5P",
        "Docking Station Thunderbolt",
        True,
        "1.0",
        K,
    ),
    (
        "A-10103",
        C.DOCKING_STATION,
        "Dock U3",
        "KD-DOCK-U3",
        "Docking Station USB-C (previous)",
        False,
        "0.8",
        K,
    ),
    ("A-10201", C.MONITOR, "View 24F Monitor", "LM-VIEW-24F", "Monitor 24", True, "4.5", L),
    ("A-10202", C.MONITOR, "View 27Q Monitor", "LM-VIEW-27Q", "Monitor 27", True, "6.2", L),
    (
        "A-10203",
        C.MONITOR,
        "View 27Q-C Monitor",
        "LM-VIEW-27QC",
        "Monitor 27 USB-C",
        True,
        "6.4",
        L,
    ),
    (
        "A-10204",
        C.MONITOR,
        "View 34W Curved Monitor",
        "LM-VIEW-34W",
        "Monitor 34 Ultrawide",
        True,
        "8.9",
        L,
    ),
    (
        "A-10205",
        C.MONITOR,
        "View 22 Monitor",
        "LM-VIEW-22",
        "Monitor 22 (previous)",
        False,
        "3.8",
        L,
    ),
    ("A-10301", C.HEADSET, "Talk H2 Mono Headset", "QA-TALK-H2", "Headset Mono", True, "0.3", Q),
    (
        "A-10302",
        C.HEADSET,
        "Talk H3 Stereo Headset",
        "QA-TALK-H3",
        "Headset Stereo",
        True,
        "0.3",
        Q,
    ),
    (
        "A-10303",
        C.HEADSET,
        "Talk H5 ANC Headset",
        "QA-TALK-H5",
        "Headset Wireless ANC",
        True,
        "0.4",
        Q,
    ),
    ("A-10304", C.ACCESSORY, "Speakerphone SP1", "QA-SP1", "Speakerphone", True, "0.4", Q),
    ("A-10401", C.KEYBOARD, "Type K1 Keyboard", "AP-TYPE-K1", "Keyboard Wired", True, "0.7", AP),
    (
        "A-10402",
        C.KEYBOARD,
        "Type K2 Wireless Keyboard",
        "AP-TYPE-K2",
        "Keyboard Wireless",
        True,
        "0.6",
        AP,
    ),
    ("A-10403", C.MOUSE, "Point M1 Mouse", "AP-POINT-M1", "Mouse Wired", True, "0.1", AP),
    (
        "A-10404",
        C.MOUSE,
        "Point M3 Wireless Mouse",
        "AP-POINT-M3",
        "Mouse Wireless",
        True,
        "0.1",
        AP,
    ),
    (
        "A-10405",
        C.KEYBOARD,
        "Combo KM2 Set",
        "AP-COMBO-KM2",
        "Keyboard and Mouse Set",
        True,
        "0.9",
        AP,
    ),
    ("A-10501", C.WEBCAM, "Lens C2 Webcam", "LM-LENS-C2", "Webcam HD", True, "0.2", L),
    ("A-10502", C.WEBCAM, "Lens C4 4K Webcam", "LM-LENS-C4", "Webcam 4K", True, "0.2", L),
    ("A-10601", C.BAG, "Carry 14 Sleeve", "PW-CARRY-14", "Laptop Sleeve 14", True, "0.3", PW),
    ("A-10602", C.BAG, "Carry 16 Backpack", "PW-CARRY-16", "Laptop Backpack 16", True, "0.9", PW),
    (
        "A-10701",
        C.ACCESSORY,
        "Power 65W USB-C Charger",
        "AP-PWR-65",
        "Charger 65W",
        True,
        "0.3",
        AP,
    ),
    (
        "A-10702",
        C.ACCESSORY,
        "Power 100W USB-C Charger",
        "AP-PWR-100",
        "Charger 100W",
        True,
        "0.4",
        AP,
    ),
    ("A-10703", C.ACCESSORY, "Cable USB-C 2 m", "AP-CBL-C2", "Cable USB-C", True, "0.1", AP),
    (
        "A-10704",
        C.ACCESSORY,
        "Adapter USB-C to HDMI",
        "AP-ADP-HDMI",
        "Adapter HDMI",
        True,
        "0.1",
        AP,
    ),
    (
        "A-10705",
        C.ACCESSORY,
        "Adapter USB-C to Ethernet",
        "AP-ADP-ETH",
        "Adapter Ethernet",
        True,
        "0.1",
        AP,
    ),
    (
        "A-10706",
        C.ACCESSORY,
        "Stand LS1 Laptop Stand",
        "AP-STAND-LS1",
        "Laptop Stand",
        True,
        "1.1",
        AP,
    ),
    ("A-10707", C.ACCESSORY, "Arm MA2 Monitor Arm", "AP-ARM-MA2", "Monitor Arm", True, "3.0", AP),
    ("A-10708", C.ACCESSORY, "Privacy Filter 14", "AP-PF-14", "Privacy Filter 14", True, "0.2", AP),
    ("A-10709", C.ACCESSORY, "Privacy Filter 16", "AP-PF-16", "Privacy Filter 16", True, "0.2", AP),
    ("A-10710", C.ACCESSORY, "Lock KL1 Cable Lock", "AP-LOCK-KL1", "Cable Lock", True, "0.2", AP),
    ("A-10711", C.ACCESSORY, "Presenter P1", "AP-PRES-P1", "Presenter", True, "0.1", AP),
    ("A-10712", C.ACCESSORY, "Stylus S1 Pen", "AP-STYLUS-S1", "Stylus Pen", True, "0.1", AP),
)


def article_weight(kg: str) -> Decimal:
    return Decimal(kg)


DC, DO = DeviationCategory, DeviationOutcome

#: id, code, label, category, outcome, requires_comment (SPEC §6.13)
DEVIATION_CODES: tuple[tuple[int, str, str, DeviationCategory, DeviationOutcome, bool], ...] = (
    (
        1,
        "DV01",
        "Technician unavailable (illness, vehicle breakdown)",
        DC.PROVIDER_RESCHEDULE,
        DO.RESCHEDULE,
        False,
    ),
    (2, "DV02", "Wrong hardware provided", DC.PROVIDER_RESCHEDULE, DO.RESCHEDULE, True),
    (
        3,
        "DV03",
        "New device damaged or defective on arrival",
        DC.PROVIDER_RESCHEDULE,
        DO.RESCHEDULE,
        True,
    ),
    (4, "DV04", "User not present or unreachable", DC.CUSTOMER_RESCHEDULE, DO.RESCHEDULE, False),
    (
        5,
        "DV05",
        "User requested a new date during the visit",
        DC.CUSTOMER_RESCHEDULE,
        DO.RESCHEDULE,
        False,
    ),
    (6, "DV06", "User data backup not available", DC.CUSTOMER_RESCHEDULE, DO.RESCHEDULE, False),
    (
        7,
        "DV07",
        "User rescheduled at least one working day ahead",
        DC.CUSTOMER_RESCHEDULE,
        DO.RESCHEDULE,
        False,
    ),
    (
        8,
        "DV08",
        "User rescheduled at short notice (less than 24 h)",
        DC.CUSTOMER_RESCHEDULE,
        DO.RESCHEDULE,
        False,
    ),
    (
        9,
        "DV09",
        "Site not accessible (building or room closed)",
        DC.SITE_ISSUE,
        DO.RESCHEDULE,
        True,
    ),
    (10, "DV10", "User refused to hand over the old device", DC.REFUSAL, DO.COMPLETE, True),
    (
        11,
        "DV11",
        "Old device defective — collection refused, new removal request needed",
        DC.REFUSAL,
        DO.COMPLETE,
        True,
    ),
    (
        12,
        "DV12",
        "Additional user briefing required (extra effort)",
        DC.EXTRA_EFFORT,
        DO.COMPLETE,
        False,
    ),
    (13, "DV13", "Additional peripherals connected on site", DC.EXTRA_EFFORT, DO.COMPLETE, False),
    (
        14,
        "DV14",
        "Network or infrastructure issue — installation partially completed",
        DC.SITE_ISSUE,
        DO.COMPLETE,
        True,
    ),
    (15, "DV15", "Order cancelled: incorrect order by client", DC.CANCELLATION, DO.CANCEL, True),
    (
        16,
        "DV16",
        "Order cancelled: user refused the hardware exchange",
        DC.CANCELLATION,
        DO.CANCEL,
        True,
    ),
)


@dataclass(frozen=True, slots=True)
class PersonaDef:
    email: str
    first: str
    last: str
    role: Role
    home: str | None
    order: int
    description: str
    group: str


PERSONAS: tuple[PersonaDef, ...] = (
    PersonaDef(
        "dana.dispatcher@example.com",
        "Dana",
        "Dispatcher",
        Role.DISPATCHER,
        "RIV",
        1,
        "Runs imports, comms, staging, tour planning and call-offs",
        "primary",
    ),
    PersonaDef(
        "theo.technician@example.com",
        "Theo",
        "Technician",
        Role.TECHNICIAN,
        "RIV",
        2,
        "Has a Riverton tour today with 5 stops",
        "primary",
    ),
    PersonaDef(
        "vic.viewer@example.com",
        "Vic",
        "Viewer",
        Role.VIEWER,
        None,
        3,
        "Client-side observer for Globex Fleet AG",
        "primary",
    ),
    PersonaDef(
        "ada.admin@example.com",
        "Ada",
        "Admin",
        Role.ADMIN,
        None,
        4,
        "Master data, users and demo reset",
        "primary",
    ),
    PersonaDef(
        "tara.lindqvist@example.com",
        "Tara",
        "Lindqvist",
        Role.TECHNICIAN,
        "HIL",
        5,
        "Hillford tours",
        "more_technicians",
    ),
    PersonaDef(
        "omar.reyes@example.com",
        "Omar",
        "Reyes",
        Role.TECHNICIAN,
        "EAS",
        6,
        "Eastgate, Northport and Southmere tours",
        "more_technicians",
    ),
    PersonaDef(
        "jonas.weber@example.com",
        "Jonas",
        "Weber",
        Role.TECHNICIAN,
        "EAS",
        7,
        "Westbay, Lakeside, Highmoor and Oakridge tours",
        "more_technicians",
    ),
)


#: Buffer per category: laptops 4 (RIV/HIL) / 3 (EAS), docks 2, monitors 2, headsets 1.
def buffer_for(category: ArticleCategory, site_code: str) -> int:
    if category == C.LAPTOP:
        return 3 if site_code == "EAS" else 4
    if category in (C.DOCKING_STATION, C.MONITOR):
        return 2
    if category == C.HEADSET:
        return 1
    return 0


def min_max_for(category: ArticleCategory) -> tuple[int | None, int | None]:
    if category == C.LAPTOP:
        return 5, 40
    if category == C.DOCKING_STATION:
        return 3, 30
    return None, None
