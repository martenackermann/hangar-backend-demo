"""H1 request/response DTOs (API.md §4). SR / lease / contact payloads come from the core
read models (SPEC_RESOLUTIONS #1); only H1-specific shapes are defined here."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import EmailStr, Field, field_validator

from app.core.enums import (
    AssetAction,
    AssetStatus,
    DeliveryType,
    Gender,
    JobStatus,
    MailStatus,
    PipelineStage,
    ServiceRequestStatus,
    Tone,
    Trigger,
)
from app.core.schemas import ApiInput, ApiModel, DownloadLink, EntityRef, LocationRef, UserRef

from .models import SETTING_RANGES

# --- service requests ------------------------------------------------------------------------------


class DeliveryAddressPatch(ApiInput):
    street: str | None = Field(default=None, max_length=160)
    zip: str | None = Field(default=None, max_length=10)
    city: str | None = Field(default=None, max_length=80)
    country: str | None = Field(default=None, max_length=60)
    room: str | None = Field(default=None, max_length=40)
    building: str | None = Field(default=None, max_length=40)


class ServiceRequestPatch(ApiInput):
    version: int | None = None
    delivery_address: DeliveryAddressPatch | None = None
    onsite_contact_phone: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=4000)
    cost_center: str | None = Field(default=None, max_length=20)
    region_id: int | None = None
    delivery_type_override: DeliveryType | None = None


# --- lease returns ---------------------------------------------------------------------------------


class LeaseReturnPatch(ApiInput):
    version: int | None = None
    escalation_email: EmailStr | None = None


class CommentCreate(ApiInput):
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("body")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The comment must not be empty.")
        return value.strip()


class LeaseReturnCommentOut(ApiModel):
    id: int
    body: str
    author: UserRef
    created_at: datetime


class CommentList(ApiModel):
    data: list[LeaseReturnCommentOut]


# --- history ---------------------------------------------------------------------------------------


class _HistoryBase(ApiModel):
    id: str
    occurred_at: datetime
    actor: UserRef | None
    trigger: Trigger


class TransitionEntry(_HistoryBase):
    kind: Literal["transition"] = "transition"
    action: str
    action_label: str
    from_status: str | None
    to_status: str
    comment: str | None
    payload: dict[str, Any] | None


class MailEntry(_HistoryBase):
    kind: Literal["mail"] = "mail"
    mail_log_id: int
    template_id: str
    subject: str | None
    to: list[str]
    status: MailStatus


class AssetRefLite(ApiModel):
    id: int
    serial_number: str
    asset_tag: str | None


class AssetMovementEntry(_HistoryBase):
    kind: Literal["asset_movement"] = "asset_movement"
    movement_id: int
    asset: AssetRefLite
    action: AssetAction
    from_status: AssetStatus | None
    to_status: AssetStatus
    from_location: LocationRef | None
    to_location: LocationRef
    reason: str | None


class CommentEntry(_HistoryBase):
    kind: Literal["comment"] = "comment"
    body: str


HistoryEntry = Annotated[
    TransitionEntry | MailEntry | AssetMovementEntry | CommentEntry, Field(discriminator="kind")
]


class HistoryList(ApiModel):
    data: list[HistoryEntry]


# --- contacts --------------------------------------------------------------------------------------


class ContactPatch(ApiInput):
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=40)
    mobile: str | None = Field(default=None, max_length=40)
    department: str | None = Field(default=None, max_length=120)
    gender: Gender | None = None
    street: str | None = Field(default=None, max_length=160)
    zip: str | None = Field(default=None, max_length=10)
    city: str | None = Field(default=None, max_length=80)
    building: str | None = Field(default=None, max_length=40)
    room: str | None = Field(default=None, max_length=40)


# --- pipeline --------------------------------------------------------------------------------------


class PipelineStatusCount(ApiModel):
    status: ServiceRequestStatus
    label: str
    tone: Tone | None
    count: int
    oldest_status_changed_at: datetime | None
    attention_count: int


class PipelineStageSummary(ApiModel):
    stage: PipelineStage
    label: str
    count: int
    statuses: list[PipelineStatusCount]


class PipelineTotals(ApiModel):
    open: int
    vip_open: int
    needs_attention: int
    completed_last_7_days: int
    cancelled_last_7_days: int
    appointments_today: int


class PipelineSummary(ApiModel):
    generated_at: datetime
    stages: list[PipelineStageSummary]
    totals: PipelineTotals


# --- communications --------------------------------------------------------------------------------


class MailLogEntry(ApiModel):
    id: int
    notification_id: str
    template_id: str
    template_name: str
    status: MailStatus
    to: list[str]
    cc: list[str]
    subject: str | None
    entity: EntityRef | None
    trigger: Trigger
    triggered_by: UserRef | None
    queued_at: datetime
    sent_at: datetime | None
    error: str | None
    attachment_count: int


class MailAttachmentOut(ApiModel):
    file_id: int
    filename: str
    content_type: str
    size_bytes: int
    download: DownloadLink


class MailLogDetail(MailLogEntry):
    body_html: str | None
    body_text: str | None
    attachments: list[MailAttachmentOut]


def _range(name: str) -> Any:
    _default, low, high = SETTING_RANGES[name]
    return Field(ge=low, le=high)


class DispatchSettingsInput(ApiInput):
    lease_mails_enabled: bool
    lease_info_days_before_retirement: int = _range("lease_info_days_before_retirement")
    lease_reminder_1_after_days: int = _range("lease_reminder_1_after_days")
    lease_reminder_2_after_days: int = _range("lease_reminder_2_after_days")
    lease_escalation_after_days: int = _range("lease_escalation_after_days")
    sr_mails_enabled: bool
    sr_invite_after_import_days: int = _range("sr_invite_after_import_days")
    sr_reminder_1_after_days: int = _range("sr_reminder_1_after_days")
    sr_reminder_2_after_days: int = _range("sr_reminder_2_after_days")
    sr_escalation_after_days: int = _range("sr_escalation_after_days")
    hardware_unavailable_mail_enabled: bool
    reschedule_mail_enabled: bool
    max_mails_per_run: int = _range("max_mails_per_run")


class DispatchSettingsOut(ApiModel):
    lease_mails_enabled: bool
    lease_info_days_before_retirement: int
    lease_reminder_1_after_days: int
    lease_reminder_2_after_days: int
    lease_escalation_after_days: int
    sr_mails_enabled: bool
    sr_invite_after_import_days: int
    sr_reminder_1_after_days: int
    sr_reminder_2_after_days: int
    sr_escalation_after_days: int
    hardware_unavailable_mail_enabled: bool
    reschedule_mail_enabled: bool
    max_mails_per_run: int
    updated_at: datetime
    updated_by: UserRef | None
    next_scheduled_run_at: datetime


class DispatchRunCreate(ApiInput):
    dry_run: bool = False


class DispatchRunError(ApiModel):
    entity_type: str
    entity_id: int
    label: str
    action: str
    message: str


class DispatchPlannedMail(ApiModel):
    entity_type: Literal["service_request", "lease_return"]
    entity_id: int
    label: str
    action: str
    template_id: str
    to: list[str]


class DispatchRunOut(ApiModel):
    id: int
    trigger: Trigger
    dry_run: bool
    status: JobStatus
    started_at: datetime
    finished_at: datetime | None
    counts: dict[str, int]
    skipped: dict[str, int]
    errors: list[DispatchRunError]
    planned: list[DispatchPlannedMail] | None
    triggered_by: UserRef | None
    job_id: int | None


class NotificationTemplateInfo(ApiModel):
    id: str
    name: str
    owner: Literal["core", "h1", "h2", "h3", "h4", "h5"]
    description: str
    trigger: str
    recipients: str
    subject_template: str
    sent_last_7_days: int
    #: entity type a preview can be rendered with (``null`` = sample context only)
    entity_type: str | None


class NotificationTemplateList(ApiModel):
    data: list[NotificationTemplateInfo]


class TemplatePreview(ApiModel):
    subject: str
    html: str
    text: str
