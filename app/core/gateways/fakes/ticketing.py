"""FakeTicketingImportSource (SPEC §8.4): deterministic ITSM-style CSV exports in the exact
§7 formats, built from the seed vocabulary. Used by H2 for ``samples/`` and the demo
"Generate a fresh ITSM export" button."""

from __future__ import annotations

import csv
import io
import random
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta

from app.core.enums import ImportType
from app.core.seed.reference import ARTICLES, REGIONS
from app.core.seed.vocabulary import (
    ASSIGNMENT_GROUPS,
    BASE32,
    DEPARTMENTS,
    FIRST_NAMES,
    LAST_NAMES,
    SOFTWARE,
    STREETS,
)
from app.core.timeutil import business_today

LEASE_COLUMNS = [
    "asset_tag",
    "serial_number",
    "model_category",
    "checked_in_date",
    "retirement_date",
    "model",
    "user_id",
    "install_status",
    "ci_name",
    "gender",
    "first_name",
    "last_name",
    "email",
    "cost_center",
    "escalation_email",
]
SR_COLUMNS = [
    "sr_number",
    "task_number",
    "itsm_state",
    "assignment_group",
    "opened_at",
    "user_id",
    "catalog_item",
    "short_description",
    "order_details",
    "delivery_address",
    "gender",
    "first_name",
    "last_name",
    "email",
    "cost_center",
]
CONTACT_COLUMNS = [
    "user_id",
    "gender",
    "first_name",
    "last_name",
    "email",
    "phone",
    "mobile",
    "department",
    "street",
    "zip",
    "city",
    "building",
    "room",
]
ASSET_COLUMNS = [
    "order_number",
    "serial_number",
    "asset_tag",
    "external_asset_id",
    "ci_name",
    "model",
    "mac_address",
    "checked_in_date",
]
SYNC_COLUMNS = [
    "serial_number",
    "install_state",
    "stockroom",
    "sr_number",
    "assigned_user_id",
    "asset_tag",
    "model",
    "order_number",
    "external_asset_id",
    "ci_name",
    "mac_address",
    "checked_in_date",
]

_ACTIVE = [a for a in ARTICLES if a[5]]
_LAPTOP_TYPES = [
    "Laptop Standard 14",
    "Laptop Performance 16",
    "Laptop Convertible 13",
    "Laptop Workstation 15",
]
_GENDER = ["female", "male", "diverse", "unspecified"]


class _Person:
    def __init__(self, rng: random.Random, user_number: int) -> None:
        self.user_id = f"E{user_number:06d}"
        self.first = rng.choice(FIRST_NAMES)
        self.last = rng.choice(LAST_NAMES)
        self.gender = rng.choice(_GENDER)
        self.email = f"{self.first.lower()}.{self.last.lower()}{user_number % 1000}@example.com"
        region = rng.choice(REGIONS)
        self.zip = f"{region.zip_prefix}{rng.randint(100, 999)}"
        self.city = region.name
        self.street = f"{rng.randint(1, 120)} {rng.choice(STREETS)}"
        self.cost_center = f"CC-{rng.randint(1000, 8999)}"


def _serial(rng: random.Random, prefix: str) -> str:
    return prefix + "".join(rng.choice(BASE32) for _ in range(8))


def _mac(rng: random.Random) -> str:
    return "02:00:5E:" + ":".join(f"{rng.randint(0, 255):02X}" for _ in range(3))


def _write(columns: list[str], rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=columns, lineterminator="\r\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return buffer.getvalue().encode("utf-8")


def packed(pairs: list[tuple[str, str]]) -> str:
    return ",".join(f"[{key}: {value}]" for key, value in pairs)


def _set_packed(value: str, key: str, new: str) -> str:
    pattern = re.compile(rf"\[{re.escape(key)}: [^\]]*\]")
    if pattern.search(value):
        return pattern.sub(f"[{key}: {new}]", value, count=1)
    return f"{value},[{key}: {new}]"


class FakeTicketingImportSource:
    def generate_export(
        self,
        import_type: ImportType,
        *,
        rows: int,
        seed: int,
        start_number: int | None = None,
        with_errors: bool = False,
    ) -> bytes:
        rng = random.Random(seed)
        today = business_today()
        builders: dict[ImportType, Callable[[], bytes]] = {
            ImportType.LEASE_RETURNS: lambda: self._leases(
                rng, rows, start_number or 150001, today, with_errors
            ),
            ImportType.SERVICE_REQUESTS: lambda: self._service_requests(
                rng, rows, start_number or 12301, today, with_errors
            ),
            ImportType.CONTACTS: lambda: self._contacts(
                rng, rows, start_number or 100501, with_errors
            ),
            ImportType.ASSETS_NEW: lambda: self._assets(
                rng, rows, start_number or 260001, today, with_errors
            ),
            ImportType.ASSET_STATUS_SYNC: lambda: self._status_sync(
                rng, rows, start_number or 170001, today, with_errors
            ),
        }
        return builders[ImportType(import_type)]()

    # --- per type ----------------------------------------------------------------------------

    def _leases(self, rng: random.Random, n: int, start: int, today: date, errors: bool) -> bytes:
        rows = []
        for i in range(n):
            person = _Person(rng, 100001 + rng.randint(0, 899))
            rows.append(
                {
                    "asset_tag": f"GX{start + i:06d}"[:8],
                    "serial_number": _serial(rng, "KD"),
                    "model_category": "Notebook",
                    "checked_in_date": (today - timedelta(days=rng.randint(900, 1300))).isoformat(),
                    "retirement_date": (today + timedelta(days=rng.randint(10, 90))).isoformat(),
                    "model": "Aero 14 G2 Laptop",
                    "user_id": person.user_id,
                    "install_status": "In use",
                    "ci_name": f"NB-{rng.randint(10000, 99999)}",
                    "gender": person.gender,
                    "first_name": person.first,
                    "last_name": person.last,
                    "email": person.email,
                    "cost_center": person.cost_center,
                    "escalation_email": "",
                }
            )
        if errors and rows:
            rows[0]["retirement_date"] = "31/12/2026"  # invalid_date
            if len(rows) > 2:
                rows[2]["email"] = ""  # email_missing warning
        return _write(LEASE_COLUMNS, rows)

    def _service_requests(
        self, rng: random.Random, n: int, start: int, today: date, errors: bool
    ) -> bytes:
        rows = []
        for i in range(n):
            person = _Person(rng, 100001 + rng.randint(0, 899))
            procurement = rng.choices(
                ["Replacement", "New hire", "Additional device", "Removal"], [70, 15, 10, 5]
            )[0]
            details: list[tuple[str, str]] = [
                ("Procurement type", procurement),
                ("Scope of delivery", "Laptop, dock, monitor"),
                ("Client component", "Standard laptop"),
            ]
            if procurement in ("Replacement", "Removal"):
                details.append(("Old base device", f"GX1{rng.randint(0, 99999):05d}"))
            if procurement != "Removal":
                details.append(
                    ("New base device type", rng.choices(_LAPTOP_TYPES, [60, 20, 10, 10])[0])
                )
                if rng.random() < 0.65:
                    details.append(
                        (
                            "New peripheral 1 type",
                            rng.choice(["Docking Station USB-C", "Docking Station Thunderbolt"]),
                        )
                    )
                if rng.random() < 0.45:
                    details.append(
                        (
                            "New peripheral 2 type",
                            rng.choice(
                                [
                                    "Monitor 27",
                                    "Monitor 24",
                                    "Monitor 34 Ultrawide",
                                    "Headset Wireless ANC",
                                ]
                            ),
                        )
                    )
            software = rng.sample(SOFTWARE, rng.randint(0, 3))
            if software:
                details.append(("Software without approval", "; ".join(software)))
            details.append(("On-site contact phone", f"+00 555 {rng.randint(1000, 9999)}"))
            address = [
                ("Room", f"{rng.randint(1, 5)}.{rng.randint(1, 40):02d}"),
                ("Building", f"B{rng.randint(1, 6)}"),
                ("Street", person.street),
                ("Zip", person.zip),
                ("City", person.city),
                ("Country", "Freeland"),
            ]
            opened = datetime.combine(
                today - timedelta(days=rng.randint(0, 3)), datetime.min.time()
            ).replace(hour=8 + rng.randint(0, 8))
            rows.append(
                {
                    "sr_number": f"SR{start + i:07d}",
                    "task_number": f"TASK{45000 + start % 1000 + i:07d}",
                    "itsm_state": rng.choice(["New", "Open", "Work in Progress"]),
                    "assignment_group": rng.choice(ASSIGNMENT_GROUPS),
                    "opened_at": opened.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "user_id": person.user_id,
                    "catalog_item": "Workplace hardware replacement"
                    if procurement == "Replacement"
                    else f"{procurement} request",
                    "short_description": f"{procurement} for {person.first} {person.last}",
                    "order_details": packed(details),
                    "delivery_address": packed(address),
                    "gender": person.gender,
                    "first_name": person.first,
                    "last_name": person.last,
                    "email": person.email,
                    "cost_center": person.cost_center,
                }
            )
        if errors and len(rows) >= 6:
            rows[1]["sr_number"] = "SR12AB"  # invalid_format
            rows[2]["delivery_address"] = rows[2]["delivery_address"].replace(
                f"[Zip: {rows[2]['delivery_address'].split('[Zip: ')[1].split(']')[0]}],", ""
            )  # packed_missing_key
            rows[3]["order_details"] += ",[Procurement type: Replacement]"  # packed_duplicate_key
            rows[4]["order_details"] = _set_packed(
                rows[4]["order_details"], "New base device type", "Laptop Ultra 15"
            )
            rows[5]["order_details"] += ",[Color: Blue]"  # packed_unknown_key
            rows[0]["email"] = ""  # email_missing
            rows[-1]["sr_number"] = rows[0]["sr_number"]  # duplicate_in_file
        return _write(SR_COLUMNS, rows)

    def _contacts(self, rng: random.Random, n: int, start: int, errors: bool) -> bytes:
        rows = []
        for i in range(n):
            person = _Person(rng, start + i)
            rows.append(
                {
                    "user_id": person.user_id,
                    "gender": person.gender,
                    "first_name": person.first,
                    "last_name": person.last,
                    "email": person.email,
                    "phone": f"+00 555 {rng.randint(1000, 9999)}",
                    "mobile": "",
                    "department": rng.choice(DEPARTMENTS),
                    "street": person.street,
                    "zip": person.zip,
                    "city": person.city,
                    "building": f"B{rng.randint(1, 6)}",
                    "room": f"{rng.randint(1, 5)}.{rng.randint(1, 40):02d}",
                }
            )
        if errors and rows:
            rows[0]["last_name"] = ""  # missing_required
        return _write(CONTACT_COLUMNS, rows)

    def _assets(self, rng: random.Random, n: int, start: int, today: date, errors: bool) -> bytes:
        rows = []
        for i in range(n):
            article = rng.choice(_ACTIVE)
            prefix = article[3].split("-")[0]
            rows.append(
                {
                    "order_number": f"PO-{today:%y%m}-{rng.randint(100, 999)}",
                    "serial_number": _serial(rng, prefix),
                    "asset_tag": f"GX{start + i:06d}"[:8],
                    "external_asset_id": f"AST{rng.randint(100000, 999999)}",
                    "ci_name": f"CI-{rng.randint(10000, 99999)}",
                    "model": article[3],
                    "mac_address": _mac(rng) if article[1] in ("laptop", "docking_station") else "",
                    "checked_in_date": (today - timedelta(days=rng.randint(0, 5))).isoformat(),
                }
            )
        if errors and len(rows) >= 2:
            rows[0]["model"] = "KD-UNKNOWN-1"  # unknown_model warning
            rows[1]["mac_address"] = "XYZ"  # invalid mac warning
        return _write(ASSET_COLUMNS, rows)

    def _status_sync(
        self, rng: random.Random, n: int, start: int, today: date, errors: bool
    ) -> bytes:
        rows = []
        states = [("In use", ""), ("In transit", ""), ("In stock", "Old device stockroom")]
        for i in range(n):
            state, stockroom = rng.choice(states)
            rows.append(
                {
                    "serial_number": _serial(rng, "KD"),
                    "install_state": state,
                    "stockroom": stockroom,
                    "sr_number": "",
                    "assigned_user_id": f"E{100001 + rng.randint(0, 419):06d}"
                    if state == "In use"
                    else "",
                    "asset_tag": f"GX{start + i:06d}"[:8],
                    "model": "KD-AERO14-G2",
                    "order_number": "",
                    "external_asset_id": f"AST{rng.randint(100000, 999999)}",
                    "ci_name": "",
                    "mac_address": "",
                    "checked_in_date": (today - timedelta(days=rng.randint(400, 900))).isoformat(),
                }
            )
        if (errors or n >= 10) and rows:
            rows[-1]["install_state"] = "Retired"  # unmapped_state warning
        return _write(SYNC_COLUMNS, rows)
