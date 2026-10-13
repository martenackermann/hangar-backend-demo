"""Service requests and their item slots (SPEC §5.1)."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin, VersionMixin, str_enum, utcnow
from app.core.enums import (
    NEW_SLOTS,
    DeliveryType,
    HoldSource,
    ItemSlot,
    OsReinstallTiming,
    ProcurementType,
    ServiceRequestStatus,
)

if TYPE_CHECKING:
    from app.core.models.assets import Asset
    from app.core.models.leases import LeaseReturn
    from app.core.models.master import Article, DeviationCode, Region, RegionRule, Site
    from app.core.models.people import Contact, User
    from app.core.models.system import StoredFile

_EMPTY_ARRAY = text("'{}'::text[]")


class ServiceRequest(IdMixin, VersionMixin, TimestampMixin, Base):
    __tablename__ = "service_requests"
    __table_args__ = (
        CheckConstraint("number ~ '^SR[0-9]{7}$'", name="ck_service_requests_number_format"),
        Index("ix_service_requests_region_id_status", "region_id", "status"),
        Index("ix_service_requests_supplying_site_id_status", "supplying_site_id", "status"),
    )

    number: Mapped[str] = mapped_column(String(10), unique=True)
    task_number: Mapped[str | None] = mapped_column(String(12), index=True)
    itsm_state: Mapped[str | None] = mapped_column(String(40))
    assignment_group: Mapped[str | None] = mapped_column(String(120))
    opened_at: Mapped[datetime | None]
    catalog_item: Mapped[str | None] = mapped_column(String(160))
    short_description: Mapped[str | None] = mapped_column(Text)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    cost_center: Mapped[str | None] = mapped_column(String(20))
    procurement_type: Mapped[ProcurementType | None] = mapped_column(str_enum(ProcurementType))
    scope_of_delivery: Mapped[str | None] = mapped_column(String(200))
    client_component: Mapped[str | None] = mapped_column(String(120))
    requested_software: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_ARRAY
    )
    added_software: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_ARRAY
    )
    installed_software: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_ARRAY
    )
    note: Mapped[str | None] = mapped_column(Text)
    onsite_contact_phone: Mapped[str | None] = mapped_column(String(40))
    delivery_room: Mapped[str | None] = mapped_column(String(40))
    delivery_building: Mapped[str | None] = mapped_column(String(40))
    delivery_street: Mapped[str | None] = mapped_column(String(160))
    delivery_zip: Mapped[str | None] = mapped_column(String(10))
    delivery_city: Mapped[str | None] = mapped_column(String(80))
    delivery_country: Mapped[str | None] = mapped_column(String(60))
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id"))
    region_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("region_rules.id", ondelete="SET NULL")
    )
    region_overridden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    supplying_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"))
    delivery_type: Mapped[DeliveryType | None] = mapped_column(str_enum(DeliveryType))
    delivery_type_override: Mapped[DeliveryType | None] = mapped_column(str_enum(DeliveryType))
    is_campus: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[ServiceRequestStatus] = mapped_column(str_enum(ServiceRequestStatus), index=True)
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow
    )
    resume_status: Mapped[ServiceRequestStatus | None] = mapped_column(
        str_enum(ServiceRequestStatus)
    )
    communication_date: Mapped[date | None] = mapped_column(index=True)
    communication_paused: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    communication_pause_reason: Mapped[str | None] = mapped_column(String(500))
    communication_resume_date: Mapped[date | None]
    hardware_unavailable_notified_at: Mapped[datetime | None]
    #: SPEC_RESOLUTIONS #37: only import holds raise ``hold_group_left``.
    hold_source: Mapped[HoldSource | None] = mapped_column(str_enum(HoldSource, length=16))
    editor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    claimed_at: Mapped[datetime | None]
    without_asset_prep: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    os_reinstall: Mapped[OsReinstallTiming | None] = mapped_column(str_enum(OsReinstallTiming))
    appointment_start_at: Mapped[datetime | None] = mapped_column(index=True)
    appointment_end_at: Mapped[datetime | None]
    appointment_calendar_id: Mapped[int | None] = mapped_column(Integer)  # H4 table, no FK
    appointment_calendar_name: Mapped[str | None] = mapped_column(String(120))
    deviation_code_id: Mapped[int | None] = mapped_column(
        SmallInteger, ForeignKey("deviation_codes.id")
    )
    deviation_comment: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime]
    last_imported_at: Mapped[datetime]
    last_import_job_id: Mapped[int | None] = mapped_column(Integer)  # H2 id, no FK
    personalized_at: Mapped[datetime | None]
    order_closed_at: Mapped[datetime | None]
    provided_at: Mapped[datetime | None]
    shipped_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    protocol_file_id: Mapped[int | None] = mapped_column(ForeignKey("stored_files.id"))

    contact: Mapped[Contact] = relationship(lazy="selectin")
    region: Mapped[Region | None] = relationship(lazy="selectin")
    region_rule: Mapped[RegionRule | None] = relationship(lazy="selectin")
    supplying_site: Mapped[Site | None] = relationship(lazy="selectin")
    editor: Mapped[User | None] = relationship(lazy="selectin")
    deviation_code: Mapped[DeviationCode | None] = relationship(lazy="selectin")
    protocol_file: Mapped[StoredFile | None] = relationship(lazy="raise")
    items: Mapped[list[ServiceRequestItem]] = relationship(
        back_populates="service_request",
        lazy="selectin",
        order_by="ServiceRequestItem.slot",
        cascade="all, delete-orphan",
    )
    lease_returns: Mapped[list[LeaseReturn]] = relationship(
        back_populates="service_request", lazy="raise"
    )

    # --- convenience ------------------------------------------------------------------
    @property
    def effective_delivery_type(self) -> DeliveryType | None:
        return self.delivery_type_override or self.delivery_type

    @property
    def new_items(self) -> list[ServiceRequestItem]:
        return [i for i in self.items if i.slot in NEW_SLOTS]

    @property
    def old_items(self) -> list[ServiceRequestItem]:
        return [i for i in self.items if i.slot not in NEW_SLOTS]

    def item(self, slot: ItemSlot | str) -> ServiceRequestItem | None:
        return next((i for i in self.items if i.slot == slot), None)


class ServiceRequestItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "service_request_items"
    __table_args__ = (
        UniqueConstraint("service_request_id", "slot"),
        CheckConstraint(
            "left(slot, 4) <> 'new_' OR requested_type IS NOT NULL",
            name="ck_service_request_items_new_has_type",
        ),
        CheckConstraint(
            "left(slot, 4) <> 'old_' OR expected_asset_tag IS NOT NULL OR asset_id IS NOT NULL",
            name="ck_service_request_items_old_has_tag",
        ),
    )

    service_request_id: Mapped[int] = mapped_column(
        ForeignKey("service_requests.id", ondelete="CASCADE")
    )
    slot: Mapped[ItemSlot] = mapped_column(str_enum(ItemSlot))
    requested_type: Mapped[str | None] = mapped_column(String(120))
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"))
    expected_asset_tag: Mapped[str | None] = mapped_column(String(20))
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), index=True)
    assigned_at: Mapped[datetime | None]
    assigned_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    packed_at: Mapped[datetime | None]
    packed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    verified_at: Mapped[datetime | None]
    verified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    verified_serial: Mapped[str | None] = mapped_column(String(40))

    service_request: Mapped[ServiceRequest] = relationship(back_populates="items", lazy="selectin")
    article: Mapped[Article | None] = relationship(lazy="selectin")
    asset: Mapped[Asset | None] = relationship(lazy="selectin", foreign_keys=[asset_id])
    assigned_by_user: Mapped[User | None] = relationship(
        lazy="selectin", foreign_keys=[assigned_by]
    )
    packed_by_user: Mapped[User | None] = relationship(lazy="selectin", foreign_keys=[packed_by])
    verified_by_user: Mapped[User | None] = relationship(
        lazy="selectin", foreign_keys=[verified_by]
    )

    @property
    def is_new(self) -> bool:
        return self.slot in NEW_SLOTS


Index(
    "ix_service_requests_search_trgm",
    (ServiceRequest.number + " " + func.coalesce(ServiceRequest.task_number, "")).label("search"),
    postgresql_using="gin",
    postgresql_ops={"search": "gin_trgm_ops"},
)
