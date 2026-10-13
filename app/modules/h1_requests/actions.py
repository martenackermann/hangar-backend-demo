"""Payload models + handlers of the H1 actions that need more than a pure transition
(API.md §3.8 bodies). Pure transitions (chain mails, holds, VIP release, cancel, reopen,
close) are registered without a handler; their effects live in :mod:`.subscribers`."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actions import ActionBody
from app.core.enums import ServiceRequestStatus, Trigger
from app.core.errors import ValidationProblem
from app.core.models import LeaseReturn, ServiceRequest, User
from app.core.services import link_lease_return, unlink_lease_return
from app.core.timeutil import business_today
from app.core.transitions import transition_service

SR_NUMBER = re.compile(r"^SR[0-9]{7}$")


class PauseBody(ActionBody):
    """``{ reason, resumeDate? }`` — with a date the chain resumes then, else indefinitely."""

    reason: str = Field(min_length=1, max_length=500)
    resume_date: date | None = None

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A reason is required.")
        return value


class LinkServiceRequestBody(ActionBody):
    service_request_number: str = Field(min_length=1, max_length=20)

    @field_validator("service_request_number")
    @classmethod
    def _normalize(cls, value: str) -> str:
        value = value.strip().upper()
        if not SR_NUMBER.match(value):
            raise ValueError("Enter a service request number like SR0012345.")
        return value


async def pause_communication(
    session: AsyncSession, entity: Any, actor: User, payload: ActionBody
) -> None:
    assert isinstance(payload, PauseBody)
    if payload.resume_date is not None and payload.resume_date <= business_today():
        raise ValidationProblem({"resumeDate": ["The resume date must be in the future."]})
    # transition first (permission, status check), then the field changes
    await transition_service.apply(
        session,
        entity,
        "pause_communication",
        actor=actor,
        trigger=Trigger.USER,
        payload=payload,
        comment=payload.comment,
    )
    if payload.resume_date is not None:
        entity.communication_resume_date = payload.resume_date
        entity.communication_paused = False
    else:
        entity.communication_paused = True
    entity.communication_pause_reason = payload.reason


async def resume_communication(
    session: AsyncSession, entity: Any, actor: User, payload: ActionBody
) -> None:
    await transition_service.apply(
        session,
        entity,
        "resume_communication",
        actor=actor,
        trigger=Trigger.USER,
        payload=payload,
        comment=payload.comment,
    )
    entity.communication_paused = False
    entity.communication_resume_date = business_today()
    entity.communication_pause_reason = None


async def link_service_request(
    session: AsyncSession, lease: LeaseReturn, actor: User, payload: ActionBody
) -> None:
    assert isinstance(payload, LinkServiceRequestBody)
    number = payload.service_request_number
    sr = (
        await session.scalars(select(ServiceRequest).where(ServiceRequest.number == number))
    ).first()
    if sr is None:
        raise ValidationProblem(
            {"serviceRequestNumber": [f"Service request {number} does not exist."]}
        )
    if sr.status == ServiceRequestStatus.CANCELLED:
        raise ValidationProblem(
            {"serviceRequestNumber": [f"Service request {number} is cancelled."]}
        )
    await link_lease_return(
        session, lease, sr, actor=actor, trigger=Trigger.USER, comment=payload.comment
    )


async def unlink_service_request(
    session: AsyncSession, lease: LeaseReturn, actor: User, payload: ActionBody
) -> None:
    await unlink_lease_return(
        session, lease, actor=actor, trigger=Trigger.USER, comment=payload.comment
    )
