"""Declarative column specifications of the five import types (SPEC §7.3–§7.7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.enums import ImportType


class Kind(StrEnum):
    """How a cell is validated and converted (see ``values.py``)."""

    TEXT = "text"
    KEY = "key"  # upper-cased business key / code, validated by ``pattern``
    DATE = "date"
    DATETIME = "datetime"
    DATE_OR_DATETIME = "date_or_datetime"
    EMAIL = "email"  # invalid → error
    OPTIONAL_EMAIL = "optional_email"  # invalid → warning, value dropped
    GENDER = "gender"  # invalid → warning, value ignored
    ITSM_STATE = "itsm_state"  # unknown → warning, stored as given
    PROCUREMENT = "procurement"
    ZIP = "zip"  # 4–5 digits, left-padded
    MAC = "mac"  # invalid → warning, dropped
    PHONE = "phone"  # invalid → warning, dropped
    SOFTWARE = "software"  # ";"-separated list
    PACKED = "packed"


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    required: bool = False
    kind: Kind = Kind.TEXT
    max_len: int | None = None
    pattern: str | None = None
    format: str = "text"
    description: str = ""
    example: str | None = None


@dataclass(frozen=True, slots=True)
class PackedKey:
    """One ``[Key: value]`` entry of a packed column (SPEC §7.4.1)."""

    key: str
    name: str  # normalized field name used by the applier
    required: bool = False
    kind: Kind = Kind.TEXT
    max_len: int | None = None
    pattern: str | None = None
    description: str = ""
    #: required unless the row's procurement type is ``removal``
    required_unless_removal: bool = False


@dataclass(frozen=True, slots=True)
class OptionSpec:
    name: str
    required: bool
    description: str


@dataclass(frozen=True, slots=True)
class TypeSpec:
    type: ImportType
    label: str
    description: str
    signature: tuple[str, ...]
    key: str
    columns: tuple[Column, ...]
    packed: dict[str, tuple[PackedKey, ...]] = field(default_factory=dict)
    options: tuple[OptionSpec, ...] = ()

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def required_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.required]

    def column(self, name: str) -> Column:
        return next(c for c in self.columns if c.name == name)
