"""Users, saved views, contacts (client employees) and the VIP list."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin, str_enum
from app.core.enums import Gender, Role

if TYPE_CHECKING:
    from app.core.models.master import Site


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_role_is_active", "role", "is_active"),)

    email: Mapped[str] = mapped_column(String(254), unique=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    role: Mapped[Role] = mapped_column(str_enum(Role))
    home_site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    is_demo_persona: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    persona_order: Mapped[int | None] = mapped_column(SmallInteger)
    last_login_at: Mapped[datetime | None]

    home_site: Mapped[Site | None] = relationship(lazy="selectin")

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def initials(self) -> str:
        return f"{self.first_name[:1]}{self.last_name[:1]}".upper()


class SavedView(IdMixin, TimestampMixin, Base):
    __tablename__ = "saved_views"
    __table_args__ = (
        UniqueConstraint("user_id", "route", "name"),
        Index("ix_saved_views_route_is_shared", "route", "is_shared"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    route: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(80))
    search: Mapped[dict[str, Any]] = mapped_column(JSONB)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")

    user: Mapped[User] = relationship(lazy="selectin")


class Contact(IdMixin, TimestampMixin, Base):
    __tablename__ = "contacts"
    user_id: Mapped[str] = mapped_column(String(16), unique=True)
    gender: Mapped[Gender] = mapped_column(
        str_enum(Gender), default=Gender.UNSPECIFIED, server_default=Gender.UNSPECIFIED.value
    )
    first_name: Mapped[str | None] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(40))
    mobile: Mapped[str | None] = mapped_column(String(40))
    department: Mapped[str | None] = mapped_column(String(120))
    street: Mapped[str | None] = mapped_column(String(160))
    zip: Mapped[str | None] = mapped_column(String(10))
    city: Mapped[str | None] = mapped_column(String(80))
    building: Mapped[str | None] = mapped_column(String(40))
    room: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}" if self.first_name else self.last_name


class VipEntry(IdMixin, TimestampMixin, Base):
    __tablename__ = "vip_entries"

    user_id: Mapped[str] = mapped_column(String(16), unique=True)
    first_name: Mapped[str | None] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    vip_type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(500))


Index(
    "ix_contacts_search_trgm",
    (
        func.coalesce(Contact.first_name, "")
        + " "
        + Contact.last_name
        + " "
        + func.coalesce(Contact.email, "")
    ).label("search"),
    postgresql_using="gin",
    postgresql_ops={"search": "gin_trgm_ops"},
)
