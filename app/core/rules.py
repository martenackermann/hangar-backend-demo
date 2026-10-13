"""Small shared business rules and normalizers (SPEC §0 "Normalization", §6.15, §4.10)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.core.enums import Gender

_WS = re.compile(r"\s+")
_PLACE = re.compile(r"^[A-Z]{1,2}[0-9]{1,2}-[0-9]{1,3}$")


def normalize_serial(value: str | None) -> str | None:
    """Serial numbers and asset tags: trimmed, upper-cased; empty → ``None``."""
    if value is None:
        return None
    cleaned = value.strip().upper()
    return cleaned or None


normalize_asset_tag = normalize_serial


def normalize_zip(value: str | None) -> str | None:
    """Digits only, 4 digits left-padded to 5; anything else non-5-digit → ``None``."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 4:
        digits = "0" + digits
    return digits if len(digits) == 5 else None


def normalize_city(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _WS.sub(" ", value.strip()).casefold()
    return cleaned or None


def normalize_cost_center(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().upper()
    return cleaned or None


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def normalize_place(value: str | None) -> str | None:
    """Installation place: upper-cased, ``LOC:`` prefix stripped; ``None`` if invalid."""
    if not value:
        return None
    cleaned = value.strip().upper()
    if cleaned.startswith("LOC:"):
        cleaned = cleaned[4:].strip()
    return cleaned if _PLACE.match(cleaned) else None


def salutation(gender: Gender | str | None, first_name: str | None, last_name: str) -> str:
    """ "Dear Ms {last}", "Dear Mr {last}", otherwise "Dear {first} {last}" (SPEC §4.10)."""
    if gender == Gender.FEMALE:
        return f"Dear Ms {last_name}"
    if gender == Gender.MALE:
        return f"Dear Mr {last_name}"
    full = f"{first_name} {last_name}" if first_name else last_name
    return f"Dear {full}"


@dataclass(frozen=True, slots=True)
class SoftwareChecklist:
    required: list[str]
    installed: list[str]
    added: list[str]
    missing: list[str]
    complete: bool


def _dedupe_casefold(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def software_checklist(
    requested: Iterable[str], added: Iterable[str], installed: Iterable[str]
) -> SoftwareChecklist:
    """SPEC §6.15: complete iff every required entry is contained in ``installed``."""
    added_list = _dedupe_casefold(added)
    required = _dedupe_casefold([*requested, *added_list])
    installed_list = _dedupe_casefold(installed)
    installed_keys = {v.casefold() for v in installed_list}
    missing = [r for r in required if r.casefold() not in installed_keys]
    return SoftwareChecklist(required, installed_list, added_list, missing, not missing)
