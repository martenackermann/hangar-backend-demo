"""Cross-cutting system tables: status transition audit, stored files, background jobs, and
the fake booking provider's sandbox (core-owned because the fake gateway is core)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, BigIdMixin, IdMixin, TimestampMixin, str_enum, utcnow
from app.core.enums import AppointmentStatus, JobKind, JobStatus, Trigger

if TYPE_CHECKING:
    from app.core.models.people import User

#: Global sequence of the fake booking provider (sync token = last seen value).
BOOKING_SANDBOX_SEQ = Sequence("booking_sandbox_change_seq", metadata=Base.metadata)
#: Shipment numbering of the fake parcel carrier (SPEC §8.2).
FAKE_CARRIER_SEQ = Sequence("fake_carrier_seq", metadata=Base.metadata)


class StatusTransition(BigIdMixin, TimestampMixin, Base):
    """Append-only audit of every SR / lease return transition (written by TransitionService)."""

    __tablename__ = "status_transitions"
    __table_args__ = (
        Index(
            "ix_status_transitions_entity",
            "entity_type",
            "entity_id",
            text("occurred_at DESC"),
        ),
        Index("ix_status_transitions_to_status", "entity_type", "to_status", "occurred_at"),
    )

    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(48))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    trigger: Mapped[Trigger] = mapped_column(str_enum(Trigger))
    comment: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow
    )

    actor: Mapped[User | None] = relationship(lazy="selectin")


class StoredFile(IdMixin, TimestampMixin, Base):
    __tablename__ = "stored_files"
    __table_args__ = (Index("ix_stored_files_entity", "entity_type", "entity_id"),)

    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    filename: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(CHAR(64))
    purpose: Mapped[str] = mapped_column(String(40))
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime | None] = mapped_column(index=True)


class BackgroundJob(IdMixin, TimestampMixin, Base):
    """Progress/result record behind every ``202 {jobId}`` (``GET /jobs/{id}``)."""

    __tablename__ = "background_jobs"
    __table_args__ = (Index("ix_background_jobs_kind_created_at", "kind", text("created_at DESC")),)

    kind: Mapped[JobKind] = mapped_column(str_enum(JobKind))
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus),
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
        index=True,
    )
    progress_processed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    progress_total: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(String(500))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    creator: Mapped[User | None] = relationship(lazy="selectin")


class BookingSandboxAppointment(IdMixin, TimestampMixin, Base):
    """State of :class:`~app.core.gateways.fakes.booking.FakeBookingProvider` only.

    Never read by application code outside the fake (SPEC §5.5 / §8.1).
    """

    __tablename__ = "booking_sandbox_appointments"

    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    calendar_external_id: Mapped[str] = mapped_column(String(64), index=True)
    reference: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[AppointmentStatus] = mapped_column(str_enum(AppointmentStatus))
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    contact_name: Mapped[str | None] = mapped_column(String(160))
    contact_email: Mapped[str | None] = mapped_column(String(254))
    street: Mapped[str | None] = mapped_column(String(160))
    town: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    change_seq: Mapped[int] = mapped_column(
        BigInteger, BOOKING_SANDBOX_SEQ, server_default=BOOKING_SANDBOX_SEQ.next_value(), index=True
    )
