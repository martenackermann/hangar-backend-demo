"""Gateway protocols and data classes — SPEC §8 (verbatim contract).

All data classes are frozen, slotted; datetimes are timezone-aware UTC.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol

from app.core.enums import ImportType

# --- 8.1 BookingProvider -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderCalendar:
    external_id: str
    name: str
    enabled: bool
    calendar_type: str | None


@dataclass(frozen=True, slots=True)
class ProviderService:
    external_id: str
    name: str
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class ProviderAppointment:
    external_id: str
    calendar_external_id: str
    start_at: datetime
    end_at: datetime
    status: Literal["booked", "cancelled"]
    reference: str | None
    title: str | None
    contact_name: str | None
    contact_email: str | None
    street: str | None
    town: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AppointmentChange:
    kind: Literal["created", "updated", "cancelled"]
    appointment: ProviderAppointment


@dataclass(frozen=True, slots=True)
class AppointmentChanges:
    changes: list[AppointmentChange]
    next_sync_token: str
    full_resync_required: bool


@dataclass(frozen=True, slots=True)
class CreateAppointmentRequest:
    calendar_external_id: str
    service_id: str
    start_at: datetime
    end_at: datetime
    reference: str
    contact_name: str
    contact_email: str | None
    street: str | None
    town: str | None
    notes: str | None


class BookingProvider(Protocol):
    async def list_calendars(self) -> list[ProviderCalendar]: ...
    async def list_calendar_services(self, calendar_external_id: str) -> list[ProviderService]: ...
    def list_appointments(
        self, calendar_external_id: str, *, since: date, until: date | None = None
    ) -> AsyncIterator[ProviderAppointment]: ...
    async def sync_appointments(self, sync_token: str | None) -> AppointmentChanges: ...
    async def create_appointment(
        self, request: CreateAppointmentRequest
    ) -> ProviderAppointment: ...
    async def cancel_appointment(self, external_id: str, *, reason: str | None = None) -> None: ...
    def build_booking_link(
        self, *, destination_id: str, service_id: str, reference: str
    ) -> str: ...


class BookingProviderError(Exception):
    """Booking provider failure → ``502 booking_error`` at the API boundary."""


# --- 8.2 ParcelCarrier ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PostalAddress:
    name: str
    name2: str | None
    street: str
    zip: str
    city: str
    country: str
    email: str | None


@dataclass(frozen=True, slots=True)
class Parcel:
    weight_kg: Decimal
    references: list[str]


@dataclass(frozen=True, slots=True)
class ShipmentRequest:
    reference: str
    sender: PostalAddress
    recipient: PostalAddress
    parcels: list[Parcel]
    return_parcels: list[Parcel]
    return_address: PostalAddress | None
    label_format: Literal["A4", "A6"] = "A4"


@dataclass(frozen=True, slots=True)
class ShipmentResult:
    carrier_shipment_id: str
    parcel_ids: list[str]
    tracking_urls: list[str]
    return_tracking_urls: list[str]
    label_pdf: bytes


class ParcelCarrier(Protocol):
    async def create_shipment(self, request: ShipmentRequest) -> ShipmentResult: ...
    async def get_label(self, carrier_shipment_id: str) -> bytes: ...


class CarrierError(Exception):
    """Carrier failure → ``502 carrier_error`` (problem detail carries the message)."""

    def __init__(self, message: str, *, code: str = "carrier_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# --- 8.3 Mailer ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MailAttachment:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class OutgoingMail:
    sender: str
    to: list[str]
    cc: list[str]
    reply_to: str | None
    subject: str
    html: str
    text: str
    attachments: list[MailAttachment] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SentMail:
    message_id: str
    accepted: list[str]


class Mailer(Protocol):
    async def send(self, mail: OutgoingMail) -> SentMail: ...


class MailerError(Exception):
    """Delivery failure (retried by ``notifications.send``)."""


# --- 8.4 TicketingImportSource -------------------------------------------------------------


class TicketingImportSource(Protocol):
    def generate_export(
        self,
        import_type: ImportType,
        *,
        rows: int,
        seed: int,
        start_number: int | None = None,
        with_errors: bool = False,
    ) -> bytes: ...
