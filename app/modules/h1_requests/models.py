"""H1 tables (SPEC §5.2): ``dispatch_settings`` (singleton), ``dispatch_runs``, ``mail_log``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, BigIdMixin, IdMixin, TimestampMixin, str_enum
from app.core.enums import JobStatus, MailStatus, Trigger
from app.core.models import User

_EMPTY_TEXT_ARRAY = text("'{}'::text[]")
_EMPTY_INT_ARRAY = text("'{}'::integer[]")

#: (column, default, min, max) — SPEC §5.2 ranges, enforced by CHECKs and the PUT schema.
SETTING_RANGES: dict[str, tuple[int, int, int]] = {
    "lease_info_days_before_retirement": (42, 1, 365),
    "lease_reminder_1_after_days": (7, 1, 60),
    "lease_reminder_2_after_days": (7, 1, 60),
    "lease_escalation_after_days": (7, 1, 60),
    "sr_invite_after_import_days": (1, 0, 30),
    "sr_reminder_1_after_days": (5, 1, 60),
    "sr_reminder_2_after_days": (5, 1, 60),
    "sr_escalation_after_days": (5, 1, 60),
    "max_mails_per_run": (500, 1, 5000),
}


def _int_setting(name: str) -> Mapped[int]:
    default = SETTING_RANGES[name][0]
    return mapped_column(Integer, default=default, server_default=str(default))


def _flag() -> Mapped[bool]:
    return mapped_column(Boolean, default=True, server_default="true")


class DispatchSettings(TimestampMixin, Base):
    """Singleton (``id = 1``) driving the communication chain (SPEC §6.3)."""

    __tablename__ = "dispatch_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_dispatch_settings_singleton"),
        *(
            CheckConstraint(f"{col} BETWEEN {lo} AND {hi}", name=f"ck_dispatch_settings_{col}")
            for col, (_d, lo, hi) in SETTING_RANGES.items()
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, autoincrement=False)
    lease_mails_enabled: Mapped[bool] = _flag()
    lease_info_days_before_retirement: Mapped[int] = _int_setting(
        "lease_info_days_before_retirement"
    )
    lease_reminder_1_after_days: Mapped[int] = _int_setting("lease_reminder_1_after_days")
    lease_reminder_2_after_days: Mapped[int] = _int_setting("lease_reminder_2_after_days")
    lease_escalation_after_days: Mapped[int] = _int_setting("lease_escalation_after_days")
    sr_mails_enabled: Mapped[bool] = _flag()
    sr_invite_after_import_days: Mapped[int] = _int_setting("sr_invite_after_import_days")
    sr_reminder_1_after_days: Mapped[int] = _int_setting("sr_reminder_1_after_days")
    sr_reminder_2_after_days: Mapped[int] = _int_setting("sr_reminder_2_after_days")
    sr_escalation_after_days: Mapped[int] = _int_setting("sr_escalation_after_days")
    hardware_unavailable_mail_enabled: Mapped[bool] = _flag()
    reschedule_mail_enabled: Mapped[bool] = _flag()
    max_mails_per_run: Mapped[int] = _int_setting("max_mails_per_run")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    updater: Mapped[User | None] = relationship(lazy="selectin")


class DispatchRun(IdMixin, TimestampMixin, Base):
    """One run of the communication chain (scheduled 07:00, manual, or dry run)."""

    __tablename__ = "dispatch_runs"
    __table_args__ = (Index("ix_dispatch_runs_dry_run_started_at", "dry_run", "started_at"),)

    background_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="SET NULL")
    )
    trigger: Mapped[Trigger] = mapped_column(str_enum(Trigger))
    dry_run: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus))
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    skipped: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    errors: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default="[]")
    planned: Mapped[list[Any] | None] = mapped_column(JSONB)
    triggered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    triggerer: Mapped[User | None] = relationship(lazy="selectin")


class MailLog(BigIdMixin, TimestampMixin, Base):
    """Every queued notification and its delivery outcome (written by H1 subscribers)."""

    __tablename__ = "mail_log"
    __table_args__ = (
        Index("ix_mail_log_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_mail_log_template_id_queued_at", "template_id", text("queued_at DESC")),
        Index("ix_mail_log_queued_at", "queued_at"),
    )

    notification_id: Mapped[str] = mapped_column(String(36), unique=True)
    template_id: Mapped[str] = mapped_column(String(48))
    status: Mapped[MailStatus] = mapped_column(str_enum(MailStatus), index=True)
    to_addresses: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY
    )
    cc_addresses: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY
    )
    subject: Mapped[str | None] = mapped_column(String(300))
    body_html: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    entity_label: Mapped[str | None] = mapped_column(String(40))
    attachment_file_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), default=list, server_default=_EMPTY_INT_ARRAY
    )
    message_id: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[Trigger] = mapped_column(str_enum(Trigger))
    triggered_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    queued_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]

    triggerer: Mapped[User | None] = relationship(lazy="selectin")


__all__ = ["SETTING_RANGES", "DispatchRun", "DispatchSettings", "MailLog"]
