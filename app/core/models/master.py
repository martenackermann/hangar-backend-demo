"""Master / reference data: sites, stock locations, regions, rules, articles, deviation codes."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin, str_enum
from app.core.enums import (
    ArticleCategory,
    DeliveryType,
    DeviationCategory,
    DeviationOutcome,
    LocationType,
    ProcurementType,
    RegionRuleMatchKind,
    SiteKind,
)


class Site(IdMixin, TimestampMixin, Base):
    __tablename__ = "sites"

    code: Mapped[str] = mapped_column(String(8), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[SiteKind] = mapped_column(str_enum(SiteKind))
    parent_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"))
    street: Mapped[str | None] = mapped_column(String(160))
    zip: Mapped[str | None] = mapped_column(String(10))
    city: Mapped[str | None] = mapped_column(String(80))
    country: Mapped[str] = mapped_column(String(60), default="Freeland", server_default="Freeland")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")

    locations: Mapped[list[StockLocation]] = relationship(
        back_populates="site", lazy="selectin", order_by="StockLocation.sort_order"
    )


class StockLocation(IdMixin, TimestampMixin, Base):
    __tablename__ = "stock_locations"
    __table_args__ = (
        UniqueConstraint("site_id", "type", postgresql_nulls_not_distinct=True),
        CheckConstraint(
            "(type IN ('in_transit', 'in_use', 'off_fleet')) = (site_id IS NULL)",
            name="ck_stock_locations_global_has_no_site",
        ),
    )

    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    type: Mapped[LocationType] = mapped_column(str_enum(LocationType))
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")

    site: Mapped[Site | None] = relationship(back_populates="locations", lazy="selectin")


class Region(IdMixin, TimestampMixin, Base):
    __tablename__ = "regions"

    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    supplying_site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    default_delivery_type: Mapped[DeliveryType] = mapped_column(str_enum(DeliveryType))
    booking_destination_id: Mapped[str | None] = mapped_column(String(64))
    booking_service_id: Mapped[str | None] = mapped_column(String(64))
    pickup_point_name: Mapped[str | None] = mapped_column(String(120))
    pickup_point_street: Mapped[str | None] = mapped_column(String(160))
    pickup_point_zip: Mapped[str | None] = mapped_column(String(10))
    pickup_point_city: Mapped[str | None] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")

    supplying_site: Mapped[Site] = relationship(lazy="selectin")


class RegionRule(IdMixin, TimestampMixin, Base):
    __tablename__ = "region_rules"
    __table_args__ = (
        UniqueConstraint(
            "match_kind", "pattern", "procurement_type", postgresql_nulls_not_distinct=True
        ),
        Index("ix_region_rules_match_kind_pattern", "match_kind", "pattern"),
    )

    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"))
    match_kind: Mapped[RegionRuleMatchKind] = mapped_column(str_enum(RegionRuleMatchKind))
    pattern: Mapped[str] = mapped_column(String(80))
    procurement_type: Mapped[ProcurementType | None] = mapped_column(str_enum(ProcurementType))
    delivery_type: Mapped[DeliveryType | None] = mapped_column(str_enum(DeliveryType))
    is_campus: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    priority: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    note: Mapped[str | None] = mapped_column(String(200))

    region: Mapped[Region] = relationship(lazy="selectin")


class Article(IdMixin, TimestampMixin, Base):
    __tablename__ = "articles"
    __table_args__ = (
        Index(
            "uq_articles_active_internal_description",
            func.lower(text("internal_description")),
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    number: Mapped[str] = mapped_column(String(16), unique=True)
    category: Mapped[ArticleCategory] = mapped_column(str_enum(ArticleCategory))
    name: Mapped[str] = mapped_column(String(120))
    model_number: Mapped[str] = mapped_column(String(80), unique=True)
    manufacturer: Mapped[str] = mapped_column(String(80))
    external_part_number: Mapped[str | None] = mapped_column(String(40))
    internal_description: Mapped[str] = mapped_column(String(120))
    weight_kg: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("1.00"), server_default="1.00"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class ArticleSiteSetting(IdMixin, TimestampMixin, Base):
    __tablename__ = "article_site_settings"
    __table_args__ = (
        UniqueConstraint("article_id", "site_id"),
        CheckConstraint("buffer_qty >= 0", name="ck_article_site_settings_buffer_qty_positive"),
        CheckConstraint("min_qty >= 0", name="ck_article_site_settings_min_qty_positive"),
        CheckConstraint("max_qty >= min_qty", name="ck_article_site_settings_max_ge_min"),
    )

    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    buffer_qty: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    min_qty: Mapped[int | None] = mapped_column(Integer)
    max_qty: Mapped[int | None] = mapped_column(Integer)
    stock_qty: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stock_goods_in: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stock_staging: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stock_goods_out: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    demand_qty: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    calculated_at: Mapped[datetime | None]
    last_order_date: Mapped[date | None]
    last_ordered_qty: Mapped[int | None] = mapped_column(Integer)

    article: Mapped[Article] = relationship(lazy="selectin")
    site: Mapped[Site] = relationship(lazy="selectin")


class DeviationCode(TimestampMixin, Base):
    """Reference data with fixed ids 1..16 (SPEC §6.13), seeded by ``0001_core``."""

    __tablename__ = "deviation_codes"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    code: Mapped[str] = mapped_column(String(8), unique=True)
    label: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[DeviationCategory] = mapped_column(str_enum(DeviationCategory))
    outcome: Mapped[DeviationOutcome] = mapped_column(str_enum(DeviationOutcome))
    requires_comment: Mapped[bool] = mapped_column(Boolean)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
