"""Base schema classes and the shared DTOs of API.md §1–2.

- :class:`ApiModel` — responses: camelCase aliases, accepts snake_case too, ``from_attributes``;
  every field is always serialized (``null`` for unknown values, never omitted).
- :class:`ApiInput` — request bodies: same aliases, **unknown fields rejected** (422).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.core.enums import (
    ArticleCategory,
    AssetStatus,
    DeviationOutcome,
    ItemSlot,
    JobStatus,
    LocationType,
    Role,
    ScanVerdict,
    SiteKind,
)


class ApiModel(BaseModel):
    # validate_by_name + validate_by_alias == pydantic's ``populate_by_name=True``
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        from_attributes=True,
    )


class ApiInput(ApiModel):
    """Write request bodies: unknown fields → ``422 validation_error`` (SPEC §12)."""

    model_config = ConfigDict(extra="forbid", from_attributes=False)


# --- references (API.md §2) ----------------------------------------------------------------

type EntityType = Literal[
    "service_request",
    "lease_return",
    "asset",
    "calloff_order",
    "inventory",
    "import_job",
    "tour",
    "contact",
]


class UserRef(ApiModel):
    id: int
    display_name: str
    email: str
    role: Role


class SiteRef(ApiModel):
    id: int
    code: str
    name: str
    kind: SiteKind


class RegionRef(ApiModel):
    id: int
    code: str
    name: str


class LocationRef(ApiModel):
    id: int
    code: str
    name: str
    type: LocationType
    site_id: int | None


class ArticleRef(ApiModel):
    id: int
    number: str
    name: str
    model_number: str
    internal_description: str
    category: ArticleCategory


class ContactRef(ApiModel):
    id: int
    user_id: str
    display_name: str
    email: str | None


class DeviationCodeRef(ApiModel):
    id: int
    code: str
    label: str
    outcome: DeviationOutcome


class EntityRef(ApiModel):
    type: EntityType
    id: int
    label: str


class Address(ApiModel):
    street: str | None = None
    zip: str | None = None
    city: str | None = None
    country: str | None = None


class DeliveryAddress(Address):
    room: str | None = None
    building: str | None = None


class SrRef(ApiModel):
    id: int
    number: str


class AssetSummary(ApiModel):
    id: int
    serial_number: str
    asset_tag: str | None
    article: ArticleRef | None
    model: str | None
    status: AssetStatus
    location: LocationRef
    installation_place: str | None
    service_request: SrRef | None


# --- workflow ------------------------------------------------------------------------------


class BlockedAction(ApiModel):
    action: str
    guard: str
    reason: str


class BulkFailure(ApiModel):
    id: int
    code: str
    message: str


class BulkResult(ApiModel):
    action: str
    succeeded: list[int] = Field(default_factory=list)
    failed: list[BulkFailure] = Field(default_factory=list)


# --- downloads / jobs ----------------------------------------------------------------------


class DownloadLink(ApiModel):
    url: str
    filename: str
    content_type: str
    size_bytes: int
    expires_at: datetime


class JobAccepted(ApiModel):
    job_id: int
    status: Literal["queued"] = "queued"
    import_job_id: int | None = None
    dispatch_run_id: int | None = None


# --- scans (API.md §1.8) -------------------------------------------------------------------


class UndoLink(ApiModel):
    method: Literal["POST", "DELETE"]
    href: str
    body: dict[str, Any] | None = None


class ScanProgress(ApiModel):
    done: int
    total: int


class ScanResult[T](ApiModel):
    verdict: ScanVerdict
    code: str
    message: str
    scanned_value: str
    applied: bool
    entity: T | None = None
    undo: UndoLink | None = None
    progress: ScanProgress | None = None
    can_register: bool = False
    suggested_slots: list[ItemSlot] = Field(default_factory=list)


class ListResponse[T](ApiModel):
    """Small reference lists: ``{ "data": [...] }`` without meta (API.md §1.2 "List")."""

    data: list[T]

    @classmethod
    def model_parametrized_name(cls, params: tuple[Any, ...]) -> str:
        return f"{getattr(params[0], '__name__', 'Item')}List"


__all__ = [
    "Address",
    "ApiInput",
    "ApiModel",
    "ArticleRef",
    "AssetSummary",
    "BlockedAction",
    "BulkFailure",
    "BulkResult",
    "ContactRef",
    "DeliveryAddress",
    "DeviationCodeRef",
    "DownloadLink",
    "EntityRef",
    "EntityType",
    "JobAccepted",
    "JobStatus",
    "ListResponse",
    "LocationRef",
    "RegionRef",
    "ScanProgress",
    "ScanResult",
    "SiteRef",
    "SrRef",
    "UndoLink",
    "UserRef",
]
