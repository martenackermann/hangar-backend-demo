"""Fixed constants shared by core and slices (SPEC_RESOLUTIONS #6, #25, #29)."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import REGION_MUTABLE_STATUSES, ScanVerdict, Tone

__all__ = [
    "BOOKING_CALENDARS",
    "BOOKING_SERVICE_ID",
    "REGION_MUTABLE_STATUSES",
    "SCAN_CODES",
    "SEED_INVENTORY_IDS",
    "BookingCalendarDef",
    "calendar_for_region",
]


@dataclass(frozen=True, slots=True)
class BookingCalendarDef:
    id: int
    region_code: str
    external_id: str
    name: str


BOOKING_SERVICE_ID = "svc-hw-rollout"

#: Booking calendar ids are fixed 1–9, regions sorted by name (SPEC_RESOLUTIONS #6).
#: The core seeder writes ``service_requests.appointment_calendar_id/name`` from these;
#: H4 seeds ``booking_calendars`` rows with the same ids.
BOOKING_CALENDARS: tuple[BookingCalendarDef, ...] = tuple(
    BookingCalendarDef(index, code, f"cal-{code}", f"{code.capitalize()} route")
    for index, code in enumerate(
        sorted(
            [
                "riverton",
                "hillford",
                "eastgate",
                "northport",
                "westbay",
                "southmere",
                "lakeside",
                "highmoor",
                "oakridge",
            ]
        ),
        start=1,
    )
)


def calendar_for_region(region_code: str) -> BookingCalendarDef:
    return next(c for c in BOOKING_CALENDARS if c.region_code == region_code)


#: Inventory ids the core seed refers to in ``asset_movements.source_ref`` (H5 seeds the
#: inventories with exactly these ids): Eastgate closed T−60, Riverton closed T−30,
#: Hillford open since wd(−1).
SEED_INVENTORY_IDS: dict[str, int] = {"EAS": 1, "RIV": 2, "HIL": 3}


@dataclass(frozen=True, slots=True)
class ScanCodeDef:
    verdict: ScanVerdict
    label: str
    sound: str

    @property
    def tone(self) -> Tone:
        return _VERDICT_TONE[self.verdict]


_VERDICT_TONE = {
    ScanVerdict.OK: Tone.DONE,
    ScanVerdict.WARNING: Tone.CAUTION,
    ScanVerdict.ERROR: Tone.CRITICAL,
}
_SOUND = {ScanVerdict.OK: "ok", ScanVerdict.WARNING: "warn", ScanVerdict.ERROR: "error"}


def _c(verdict: ScanVerdict, label: str) -> ScanCodeDef:
    return ScanCodeDef(verdict, label, _SOUND[verdict])


OK, WARN, ERR = ScanVerdict.OK, ScanVerdict.WARNING, ScanVerdict.ERROR

#: One table for every scan code (SPEC_RESOLUTIONS #25): banner tone and sound depend on the
#: verdict only. Published by ``GET /meta/enums`` as ``scanCode``. A code always has the same
#: verdict everywhere; H5's inventory "unknown serial, recorded" uses ``unknown_recorded``.
SCAN_CODES: dict[str, ScanCodeDef] = {
    # installation place (H3, SPEC §6.16)
    "placed": _c(OK, "Placed"),
    "unchanged": _c(OK, "Unchanged"),
    "undone": _c(OK, "Undone"),
    "place_occupied": _c(WARN, "Place occupied"),
    "moved_from_other_site": _c(WARN, "Moved from another site"),
    "unknown_model": _c(WARN, "Unknown model"),
    "invalid_place": _c(ERR, "Invalid place"),
    "not_available": _c(ERR, "Not available"),
    "not_in_stock": _c(ERR, "Not in stock"),
    # assignment (H3, SPEC §6.6)
    "assigned": _c(OK, "Assigned"),
    "not_fifo": _c(WARN, "Not first in, first out"),
    "unknown_article": _c(ERR, "Unknown article"),
    "wrong_article": _c(ERR, "Wrong article"),
    "wrong_location": _c(WARN, "Wrong location"),
    "no_installation_place": _c(ERR, "No installation place"),
    # parcel packing (H3, SPEC §6.17)
    "packed": _c(OK, "Packed"),
    "already_packed": _c(OK, "Already packed"),
    "not_on_this_request": _c(ERR, "Not on this request"),
    # inventory (H5, SPEC §6.9)
    "matched": _c(OK, "Matched"),
    "duplicate": _c(WARN, "Already scanned"),
    "unknown_recorded": _c(WARN, "Unknown serial recorded"),
    "wrong_zone": _c(WARN, "Wrong zone"),
    "wrong_site": _c(WARN, "Wrong site"),
    # deployment (H4, SPEC §6.11)
    "new_verified": _c(OK, "New device verified"),
    "old_verified": _c(OK, "Old device verified"),
    "already_verified": _c(OK, "Already verified"),
    "belongs_to_other_sr": _c(ERR, "Belongs to another request"),
    "unknown_device": _c(WARN, "Unknown device"),
    # shared
    "unknown_serial": _c(ERR, "Unknown serial"),
}
