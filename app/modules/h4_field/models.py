"""H4 tables (SPEC §5.5): booking calendars, appointments, sync state, tours, tour stops.

``booking_sandbox_appointments`` belongs to the core fake gateway (SPEC_RESOLUTIONS #5).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin, VersionMixin, str_enum
from app.core.enums import AppointmentStatus, TourStatus, TourStopOutcome

if TYPE_CHECKING:
    from app.core.models import Region, ServiceRequest, User

_EMPTY_ARRAY = text("'{}'::text[]")


class BookingCalendar(TimestampMixin, Base):
    """Provider calendar = one region route. Ids 1–9 are fixed (SPEC_RESOLUTIONS #6), so the
    identity is ``BY DEFAULT`` (the seeder inserts explicit ids, sync-created calendars
    continue the sequence)."""

    __tablename__ = "booking_calendars"

    id: Mapped[int] = mapped_column(
        Integer, Identity(always=False), primary_key=True, sort_order=-100
    )
    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id"))
    service_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_ARRAY
    )
    calendar_type: Mapped[str | None] = mapped_column(String(40))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    synced_at: Mapped[datetime | None]

    region: Mapped[Region | None] = relationship(lazy="selectin")


class Appointment(IdMixin, TimestampMixin, Base):
    """Local copy of a provider appointment (one table for synced and manual bookings).

    ``is_current`` marks the SR's current appointment; ``superseded_at`` is set when a
    reschedule left the provider booking in place (SPEC_RESOLUTIONS #7) — sync never makes
    a superseded appointment current again.
    """

    __tablename__ = "appointments"
    __table_args__ = (
        Index("ix_appointments_calendar_id_start_at", "calendar_id", "start_at"),
        Index(
            "ix_appointments_unmatched_reference",
            "reference",
            postgresql_where=text("service_request_id IS NULL"),
        ),
        CheckConstraint("source IN ('sync', 'manual')", name="ck_appointments_source"),
    )

    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("booking_calendars.id"))
    service_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("service_requests.id"), index=True
    )
    reference: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[AppointmentStatus] = mapped_column(str_enum(AppointmentStatus))
    start_at: Mapped[datetime] = mapped_column(index=True)
    end_at: Mapped[datetime]
    title: Mapped[str | None] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(160))
    contact_email: Mapped[str | None] = mapped_column(String(254))
    street: Mapped[str | None] = mapped_column(String(160))
    town: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    booked_at: Mapped[datetime | None]
    provider_updated_at: Mapped[datetime | None]
    source: Mapped[str] = mapped_column(String(16), default="sync", server_default="sync")
    last_synced_at: Mapped[datetime | None]
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    superseded_at: Mapped[datetime | None]

    calendar: Mapped[BookingCalendar] = relationship(lazy="selectin")


class BookingSyncState(TimestampMixin, Base):
    """Singleton (``id = 1``) with the provider's incremental sync token (SPEC §5.5)."""

    __tablename__ = "booking_sync_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_booking_sync_state_singleton"),)

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False, default=1, sort_order=-100
    )
    sync_token: Mapped[str | None] = mapped_column(String(200))
    last_full_sync_at: Mapped[datetime | None]
    last_incremental_sync_at: Mapped[datetime | None]
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_job_id: Mapped[int | None] = mapped_column(Integer)  # background_jobs id (no FK)


class Tour(IdMixin, TimestampMixin, VersionMixin, Base):
    """One calendar (region route) on one date, assigned to one technician (SPEC §4.7)."""

    __tablename__ = "tours"
    __table_args__ = (
        Index(
            "uq_tours_calendar_id_tour_date_active",
            "calendar_id",
            "tour_date",
            unique=True,
            postgresql_where=text("status <> 'cancelled'"),
        ),
        Index("ix_tours_technician_id_tour_date", "technician_id", "tour_date"),
    )

    calendar_id: Mapped[int] = mapped_column(ForeignKey("booking_calendars.id"))
    tour_date: Mapped[date] = mapped_column(index=True)
    technician_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[TourStatus] = mapped_column(str_enum(TourStatus))
    notes: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))

    calendar: Mapped[BookingCalendar] = relationship(lazy="selectin")
    technician: Mapped[User | None] = relationship(lazy="selectin", foreign_keys=[technician_id])
    stops: Mapped[list[TourStop]] = relationship(
        back_populates="tour",
        lazy="selectin",
        order_by="TourStop.position",
        cascade="all, delete-orphan",
    )


class TourStop(IdMixin, TimestampMixin, Base):
    __tablename__ = "tour_stops"
    __table_args__ = (
        UniqueConstraint("tour_id", "position", deferrable=True, initially="DEFERRED"),
        Index(
            "uq_tour_stops_service_request_id_pending",
            "service_request_id",
            unique=True,
            postgresql_where=text("outcome = 'pending'"),
        ),
    )

    tour_id: Mapped[int] = mapped_column(ForeignKey("tours.id", ondelete="CASCADE"), index=True)
    service_request_id: Mapped[int] = mapped_column(ForeignKey("service_requests.id"), index=True)
    position: Mapped[int] = mapped_column(SmallInteger)
    outcome: Mapped[TourStopOutcome] = mapped_column(
        str_enum(TourStopOutcome),
        default=TourStopOutcome.PENDING,
        server_default=TourStopOutcome.PENDING.value,
    )
    outcome_at: Mapped[datetime | None]

    tour: Mapped[Tour] = relationship(back_populates="stops", lazy="selectin")
    service_request: Mapped[ServiceRequest] = relationship(lazy="selectin")


__all__ = ["Appointment", "BookingCalendar", "BookingSyncState", "Tour", "TourStop"]
