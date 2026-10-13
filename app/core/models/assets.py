"""Assets and the append-only ``asset_movements`` ledger (written only by AssetService)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, BigIdMixin, IdMixin, TimestampMixin, VersionMixin, str_enum, utcnow
from app.core.enums import AssetAction, AssetSource, AssetStatus, Trigger

if TYPE_CHECKING:
    from app.core.models.master import Article, StockLocation
    from app.core.models.people import Contact, User
    from app.core.models.requests import ServiceRequest


class Asset(IdMixin, VersionMixin, TimestampMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_assets_location_id_status", "location_id", "status"),
        Index("ix_assets_article_id_location_id_status", "article_id", "location_id", "status"),
        Index(
            "ix_assets_fifo",
            "article_id",
            "location_id",
            "checked_in_at",
            "id",
            postgresql_where=text("status = 'available' AND installation_place IS NOT NULL"),
        ),
    )

    serial_number: Mapped[str] = mapped_column(String(40), unique=True)
    asset_tag: Mapped[str | None] = mapped_column(String(20), unique=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"))
    model: Mapped[str | None] = mapped_column(String(80))
    external_asset_id: Mapped[str | None] = mapped_column(String(40))
    ci_name: Mapped[str | None] = mapped_column(String(80))
    order_number: Mapped[str | None] = mapped_column(String(40))
    mac_address: Mapped[str | None] = mapped_column(String(17))
    status: Mapped[AssetStatus] = mapped_column(str_enum(AssetStatus), index=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("stock_locations.id"))
    installation_place: Mapped[str | None] = mapped_column(String(8))
    service_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("service_requests.id"), index=True
    )
    assigned_contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    checked_in_at: Mapped[datetime | None]
    source: Mapped[AssetSource] = mapped_column(str_enum(AssetSource))
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow
    )

    article: Mapped[Article | None] = relationship(lazy="selectin")
    location: Mapped[StockLocation] = relationship(lazy="selectin")
    service_request: Mapped[ServiceRequest | None] = relationship(
        lazy="selectin", foreign_keys=[service_request_id]
    )
    assigned_contact: Mapped[Contact | None] = relationship(lazy="selectin")
    movements: Mapped[list[AssetMovement]] = relationship(
        back_populates="asset", lazy="raise", order_by="AssetMovement.occurred_at.desc()"
    )


class AssetMovement(BigIdMixin, TimestampMixin, Base):
    __tablename__ = "asset_movements"
    __table_args__ = (
        Index("ix_asset_movements_asset_id_occurred_at", "asset_id", text("occurred_at DESC")),
    )

    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    action: Mapped[AssetAction] = mapped_column(str_enum(AssetAction))
    from_status: Mapped[AssetStatus | None] = mapped_column(str_enum(AssetStatus))
    to_status: Mapped[AssetStatus] = mapped_column(str_enum(AssetStatus))
    from_location_id: Mapped[int | None] = mapped_column(ForeignKey("stock_locations.id"))
    to_location_id: Mapped[int] = mapped_column(ForeignKey("stock_locations.id"))
    from_installation_place: Mapped[str | None] = mapped_column(String(8))
    to_installation_place: Mapped[str | None] = mapped_column(String(8))
    service_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("service_requests.id"), index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    trigger: Mapped[Trigger] = mapped_column(str_enum(Trigger))
    reason: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, index=True
    )

    asset: Mapped[Asset] = relationship(back_populates="movements", lazy="selectin")
    from_location: Mapped[StockLocation | None] = relationship(
        lazy="selectin", foreign_keys=[from_location_id]
    )
    to_location: Mapped[StockLocation] = relationship(
        lazy="selectin", foreign_keys=[to_location_id]
    )
    service_request: Mapped[ServiceRequest | None] = relationship(lazy="selectin")
    actor: Mapped[User | None] = relationship(lazy="selectin")


Index(
    "ix_assets_search_trgm",
    (Asset.serial_number + " " + func.coalesce(Asset.asset_tag, "")).label("search"),
    postgresql_using="gin",
    postgresql_ops={"search": "gin_trgm_ops"},
)
