"""H3 table ``shipments`` (SPEC §5.4) — one row per parcel shipment created for an SR."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from app.core.models import ServiceRequest, StoredFile, User

_EMPTY_ARRAY = text("'{}'::text[]")


class Shipment(IdMixin, TimestampMixin, Base):
    __tablename__ = "shipments"
    __table_args__ = (Index("ix_shipments_created_at", text("created_at DESC")),)

    service_request_id: Mapped[int] = mapped_column(ForeignKey("service_requests.id"), index=True)
    carrier_shipment_id: Mapped[str] = mapped_column(String(40), unique=True)
    parcel_count: Mapped[int] = mapped_column(SmallInteger)
    tracking_urls: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_ARRAY
    )
    return_tracking_urls: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_ARRAY
    )
    include_return_label: Mapped[bool] = mapped_column(Boolean)
    combined_parcel: Mapped[bool] = mapped_column(Boolean)
    confirmation_mail_sent: Mapped[bool] = mapped_column(Boolean)
    recipient_name: Mapped[str] = mapped_column(String(160))
    delivery_street: Mapped[str] = mapped_column(String(160))
    delivery_zip: Mapped[str] = mapped_column(String(10))
    delivery_city: Mapped[str] = mapped_column(String(80))
    delivery_country: Mapped[str] = mapped_column(String(60))
    label_file_id: Mapped[int] = mapped_column(ForeignKey("stored_files.id"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))

    service_request: Mapped[ServiceRequest] = relationship(lazy="selectin")
    label_file: Mapped[StoredFile] = relationship(lazy="selectin")
    creator: Mapped[User] = relationship(lazy="selectin", foreign_keys=[created_by])
