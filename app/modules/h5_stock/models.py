"""H5 tables (SPEC §5.6): inventories + scans, call-off orders + lines, deactivation batches.

Two columns go beyond the SPEC table (documented in README.md):

- ``inventories.reconciliation`` — the reconciliation frozen on close (the expected set is
  evaluated live while open, so a closed inventory needs a snapshot to stay reproducible);
- ``deactivation_batches.closed_lease_return_count`` / ``sample`` — the parts of
  ``DeactivationResult`` that cannot be recomputed after the upload.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, BigIdMixin, IdMixin, TimestampMixin, VersionMixin, str_enum
from app.core.enums import (
    CallOffOrderStatus,
    DeactivationReason,
    InventoryScanResult,
    InventoryStatus,
)

if TYPE_CHECKING:
    from app.core.models import Article, Asset, Site, StockLocation, User


class Inventory(IdMixin, VersionMixin, TimestampMixin, Base):
    """Stock-take per site (hubs and Campus, SPEC_RESOLUTIONS #28)."""

    __tablename__ = "inventories"
    __table_args__ = (
        Index(
            "uq_inventories_open_site",
            "site_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    status: Mapped[InventoryStatus] = mapped_column(str_enum(InventoryStatus), index=True)
    opened_at: Mapped[datetime]
    opened_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None]
    closed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    count_date: Mapped[date | None]
    apply_corrections: Mapped[bool | None]
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reconciliation: Mapped[list[Any] | None] = mapped_column(JSONB)
    missing_report_file_id: Mapped[int | None] = mapped_column(ForeignKey("stored_files.id"))
    wrong_location_report_file_id: Mapped[int | None] = mapped_column(ForeignKey("stored_files.id"))
    unknown_report_file_id: Mapped[int | None] = mapped_column(ForeignKey("stored_files.id"))
    note: Mapped[str | None] = mapped_column(Text)

    site: Mapped[Site] = relationship(lazy="selectin")
    opener: Mapped[User] = relationship(lazy="selectin", foreign_keys=[opened_by])
    closer: Mapped[User | None] = relationship(lazy="selectin", foreign_keys=[closed_by])


class InventoryScan(BigIdMixin, TimestampMixin, Base):
    __tablename__ = "inventory_scans"
    __table_args__ = (
        UniqueConstraint("inventory_id", "serial_number"),
        Index("ix_inventory_scans_inventory_id_scanned_at", "inventory_id", "scanned_at"),
    )

    inventory_id: Mapped[int] = mapped_column(ForeignKey("inventories.id", ondelete="CASCADE"))
    serial_number: Mapped[str] = mapped_column(String(40))
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), index=True)
    result: Mapped[InventoryScanResult] = mapped_column(str_enum(InventoryScanResult))
    scanned_location_id: Mapped[int | None] = mapped_column(ForeignKey("stock_locations.id"))
    recorded_location_id: Mapped[int | None] = mapped_column(ForeignKey("stock_locations.id"))
    scanned_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    scanned_at: Mapped[datetime]

    asset: Mapped[Asset | None] = relationship(lazy="selectin")
    scanned_location: Mapped[StockLocation | None] = relationship(
        lazy="selectin", foreign_keys=[scanned_location_id]
    )
    recorded_location: Mapped[StockLocation | None] = relationship(
        lazy="selectin", foreign_keys=[recorded_location_id]
    )
    scanner: Mapped[User] = relationship(lazy="selectin")


class CallOffOrder(IdMixin, VersionMixin, TimestampMixin, Base):
    """Replenishment order per hub to Initech Logistics (SPEC §4.8, §6.8)."""

    __tablename__ = "calloff_orders"

    number: Mapped[str] = mapped_column(String(32), unique=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    status: Mapped[CallOffOrderStatus] = mapped_column(str_enum(CallOffOrderStatus), index=True)
    delivery_date: Mapped[date]
    note: Mapped[str | None] = mapped_column(Text)
    recipient_email: Mapped[str] = mapped_column(String(254))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    sent_at: Mapped[datetime | None]
    sent_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    xlsx_file_id: Mapped[int | None] = mapped_column(ForeignKey("stored_files.id"))
    pdf_file_id: Mapped[int | None] = mapped_column(ForeignKey("stored_files.id"))

    site: Mapped[Site] = relationship(lazy="selectin")
    creator: Mapped[User] = relationship(lazy="selectin", foreign_keys=[created_by])
    sender: Mapped[User | None] = relationship(lazy="selectin", foreign_keys=[sent_by])
    lines: Mapped[list[CallOffOrderLine]] = relationship(
        back_populates="order",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="CallOffOrderLine.id",
    )


class CallOffOrderLine(IdMixin, TimestampMixin, Base):
    __tablename__ = "calloff_order_lines"
    __table_args__ = (
        UniqueConstraint("calloff_order_id", "article_id"),
        CheckConstraint("quantity >= 0", name="ck_calloff_order_lines_quantity_positive"),
    )

    calloff_order_id: Mapped[int] = mapped_column(
        ForeignKey("calloff_orders.id", ondelete="CASCADE")
    )
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    demand_qty: Mapped[int] = mapped_column(Integer)
    stock_qty: Mapped[int] = mapped_column(Integer)
    buffer_qty: Mapped[int] = mapped_column(Integer)
    suggested_qty: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)

    order: Mapped[CallOffOrder] = relationship(back_populates="lines", lazy="selectin")
    article: Mapped[Article] = relationship(lazy="selectin")


class DeactivationBatch(IdMixin, TimestampMixin, Base):
    """One deactivation upload (SPEC §6.10)."""

    __tablename__ = "deactivation_batches"

    reason: Mapped[DeactivationReason] = mapped_column(str_enum(DeactivationReason))
    original_filename: Mapped[str] = mapped_column(String(200))
    file_id: Mapped[int] = mapped_column(ForeignKey("stored_files.id"))
    total_rows: Mapped[int] = mapped_column(Integer)
    deactivated_count: Mapped[int] = mapped_column(Integer, default=0)
    already_deactivated_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    not_found_count: Mapped[int] = mapped_column(Integer, default=0)
    closed_lease_return_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    not_found_file_id: Mapped[int | None] = mapped_column(ForeignKey("stored_files.id"))
    rejected_file_id: Mapped[int | None] = mapped_column(ForeignKey("stored_files.id"))
    sample: Mapped[list[Any] | None] = mapped_column(JSONB)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))

    creator: Mapped[User] = relationship(lazy="selectin")


H5_TABLES: tuple[str, ...] = (
    "inventories",
    "inventory_scans",
    "calloff_orders",
    "calloff_order_lines",
    "deactivation_batches",
)
