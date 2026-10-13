"""Cell parsers (SPEC §7.1 "Values", "Dates", "Emails"). Each raises ``ValueError``."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.enums import Gender, ProcurementType

from .formats import ITSM_STATES, PROCUREMENT_TYPES

NULL_TOKENS = frozenset({"", "N/A", "-", "NULL"})

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATETIME = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?$"
)
#: Pragmatic RFC 5322 subset: local part of atext/dots, domain of labels with a TLD.
_EMAIL = re.compile(
    r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_PHONE = re.compile(r"^[+0-9 ()/-]{5,40}$")
_HEX12 = re.compile(r"^[0-9A-F]{12}$")

_GENDERS = {
    "female": Gender.FEMALE,
    "f": Gender.FEMALE,
    "male": Gender.MALE,
    "m": Gender.MALE,
    "diverse": Gender.DIVERSE,
    "d": Gender.DIVERSE,
    "unspecified": Gender.UNSPECIFIED,
    "u": Gender.UNSPECIFIED,
}
_ITSM = {state.casefold(): state for state in ITSM_STATES}


def is_null(value: str | None) -> bool:
    return value is None or value.strip().upper() in NULL_TOKENS


def parse_date(value: str) -> date:
    match = _DATE.match(value.strip())
    if not match:
        raise ValueError(f"'{value}' is not a date (YYYY-MM-DD).")
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError as exc:
        raise ValueError(f"'{value}' is not a valid calendar date.") from exc


def parse_datetime(value: str) -> datetime:
    """``YYYY-MM-DD[T ]HH:MM[:SS][Z|±HH:MM]``; naive values are business time (→ UTC)."""
    match = _DATETIME.match(value.strip())
    if not match:
        raise ValueError(f"'{value}' is not a date-time (YYYY-MM-DDTHH:MM[:SS][Z]).")
    year, month, day, hour, minute = (int(match[i]) for i in range(1, 6))
    second = int(match[6]) if match[6] else 0
    zone = match[7]
    if zone is None:
        tz = get_settings().tz
    elif zone == "Z":
        tz = UTC
    else:
        sign = 1 if zone[0] == "+" else -1
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        local = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    except ValueError as exc:
        raise ValueError(f"'{value}' is not a valid date-time.") from exc
    return local.astimezone(UTC)


def parse_date_or_datetime(value: str) -> datetime:
    """A date is taken as midnight business time."""
    text = value.strip()
    if _DATE.match(text):
        day = parse_date(text)
        return datetime(day.year, day.month, day.day, tzinfo=get_settings().tz).astimezone(UTC)
    return parse_datetime(text)


def parse_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise ValueError(f"'{value}' is not a valid email address.")
    return email


def parse_gender(value: str) -> Gender:
    gender = _GENDERS.get(value.strip().lower())
    if gender is None:
        raise ValueError(f"'{value}' is not a gender (female, male, diverse, unspecified).")
    return gender


def parse_itsm_state(value: str) -> tuple[str, bool]:
    """Canonical state and whether it is one of the known values."""
    known = _ITSM.get(value.strip().casefold())
    return (known, True) if known else (value.strip(), False)


def parse_procurement(value: str) -> ProcurementType:
    key = re.sub(r"[\s_]+", " ", value.strip().lower())
    found = PROCUREMENT_TYPES.get(key)
    if found is None:
        raise ValueError(
            f"'{value}' is not a procurement type (Replacement, New hire, Additional device, Removal)."
        )
    return ProcurementType(found)


def parse_zip(value: str) -> str:
    text = value.strip()
    if not re.fullmatch(r"\d{4,5}", text):
        raise ValueError(f"'{value}' is not a zip code (4–5 digits).")
    return text.zfill(5)


def parse_mac(value: str) -> str:
    digits = re.sub(r"[:\-.\s]", "", value.strip().upper())
    if not _HEX12.match(digits):
        raise ValueError(f"'{value}' is not a MAC address (12 hex digits).")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def parse_phone(value: str) -> str:
    text = value.strip()
    if not _PHONE.match(text):
        raise ValueError(f"'{value}' is not a phone number.")
    return text


def parse_software(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]
