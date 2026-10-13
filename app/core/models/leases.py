"""Lease returns and their comment history."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin, VersionMixin, str_enum, utcnow
from app.core.enums import LeaseReturnStatus

if TYPE_CHECKING:
    from app.core.models.people import Contact, User
    from app.core.models.requests import ServiceRequest


class LeaseReturn(IdMixin, VersionMixin, TimestampMixin, Base):
    __tablename__ = "lease_returns"

    asset_tag: Mapped[str] = mapped_column(String(20), unique=True)
    serial_number: Mapped[str | None] = mapped_column(String(40))
    model_category: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(120))
    checked_in_at: Mapped[datetime | None]
    retirement_date: Mapped[date] = mapped_column(index=True)
    user_id: Mapped[str] = mapped_column(String(16))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), index=True)
    install_status: Mapped[str | None] = mapped_column(String(40))
    ci_name: Mapped[str | None] = mapped_column(String(80))
    cost_center: Mapped[str | None] = mapped_column(String(20))
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_campus: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[LeaseReturnStatus] = mapped_column(str_enum(LeaseReturnStatus), index=True)
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow
    )
    communication_date: Mapped[date | None]
    communication_paused: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    communication_pause_reason: Mapped[str | None] = mapped_column(String(500))
    communication_resume_date: Mapped[date | None]
    info_sent_at: Mapped[datetime | None]
    reminder_1_sent_at: Mapped[datetime | None]
    reminder_2_sent_at: Mapped[datetime | None]
    escalation_sent_at: Mapped[datetime | None]
    escalation_email: Mapped[str | None] = mapped_column(String(254))
    service_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("service_requests.id"), index=True
    )
    linked_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    close_reason: Mapped[str | None] = mapped_column(String(200))
    imported_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    last_import_job_id: Mapped[int | None] = mapped_column(Integer)

    contact: Mapped[Contact | None] = relationship(lazy="selectin")
    service_request: Mapped[ServiceRequest | None] = relationship(
        back_populates="lease_returns", lazy="selectin"
    )
    comments: Mapped[list[LeaseReturnComment]] = relationship(
        back_populates="lease_return",
        lazy="raise",
        order_by="LeaseReturnComment.created_at.desc()",
        cascade="all, delete-orphan",
    )


class LeaseReturnComment(IdMixin, TimestampMixin, Base):
    __tablename__ = "lease_return_comments"
    __table_args__ = (
        CheckConstraint(
            "char_length(body) BETWEEN 1 AND 2000", name="ck_lease_return_comments_body_length"
        ),
    )

    lease_return_id: Mapped[int] = mapped_column(
        ForeignKey("lease_returns.id", ondelete="CASCADE"), index=True
    )
    body: Mapped[str] = mapped_column(Text)
    author_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))

    lease_return: Mapped[LeaseReturn] = relationship(back_populates="comments", lazy="selectin")
    author: Mapped[User] = relationship(lazy="selectin")
