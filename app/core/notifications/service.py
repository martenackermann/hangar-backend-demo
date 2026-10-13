"""``notify()`` and the ``notifications.send`` task (SPEC §2.4, §8.5; SPEC_RESOLUTIONS #14).

``notify()`` emits :class:`NotificationQueued` in-transaction (H1 writes ``mail_log``) and,
after commit, defers ``notifications.send``. The task renders the template, sends via the
``Mailer`` gateway and emits :class:`NotificationSent` / :class:`NotificationFailed` in
its own transaction. Retries: delays 10 s, 60 s, 300 s; after the last attempt → failed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from procrastinate import JobContext, RetryDecision
from procrastinate.jobs import Job
from procrastinate.retry import BaseRetryStrategy
from pydantic import BaseModel
from pydantic_core import to_jsonable_python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gateways
from app.core.config import get_settings
from app.core.context import current_actor_id, current_trigger, job_context
from app.core.db import after_commit, commit, open_session
from app.core.enums import Trigger
from app.core.events import (
    EntityKey,
    NotificationFailed,
    NotificationQueued,
    NotificationSent,
    emit,
)
from app.core.files import read_file
from app.core.gateways.protocols import MailAttachment, OutgoingMail
from app.core.models import StoredFile, User
from app.core.notifications.registry import templates
from app.core.notifications.renderer import render
from app.core.schemas import EntityRef

log = structlog.get_logger(__name__)

TASK_NAME = "notifications.send"
RETRY_DELAYS = (10, 60, 300)


class MailRetry(BaseRetryStrategy):
    def get_retry_decision(self, *, exception: BaseException, job: Job) -> RetryDecision | None:
        if job.attempts >= len(RETRY_DELAYS):
            return None
        return RetryDecision(
            retry_at=datetime.now(UTC) + timedelta(seconds=RETRY_DELAYS[job.attempts])
        )


def _entity_key(entity: EntityRef | EntityKey | None) -> EntityKey | None:
    if entity is None:
        return None
    if isinstance(entity, EntityKey):
        return entity
    return EntityKey(entity.type, entity.id, entity.label)


async def notify(
    session: AsyncSession,
    template_id: str,
    *,
    to: list[str],
    cc: list[str] | None = None,
    context: dict[str, Any] | BaseModel,
    entity: EntityRef | EntityKey | None = None,
    attachments: list[int] | None = None,
    trigger: Trigger | None = None,
    actor: User | int | None = None,
) -> str:
    """Queue a notification; returns its ``notification_id`` (correlation id for ``mail_log``)."""
    if template_id not in templates:
        raise KeyError(f"Unknown notification template {template_id!r}")
    recipients = [address for address in to if address]
    if not recipients:
        raise ValueError(f"notify({template_id}) needs at least one recipient")
    notification_id = str(uuid.uuid4())
    actor_id = actor.id if isinstance(actor, User) else actor
    if actor_id is None:
        actor_id = current_actor_id.get()
    key = _entity_key(entity)
    event = NotificationQueued(
        notification_id=notification_id,
        template_id=template_id,
        to=recipients,
        cc=[address for address in (cc or []) if address],
        entity=key,
        trigger=trigger or current_trigger.get(),
        actor_id=actor_id,
        attachment_file_ids=list(attachments or []),
    )
    await emit(session, event)
    payload = (
        context.model_dump(mode="json")
        if isinstance(context, BaseModel)
        else to_jsonable_python(context)
    )

    async def _defer() -> None:
        from app.jobs.app import job_app

        task = cast(Any, job_app.configure_task(TASK_NAME, queue="mail"))
        await task.defer_async(
            notification_id=notification_id,
            template_id=template_id,
            to=event.to,
            cc=event.cc,
            context=payload,
            entity={"type": key.type, "id": key.id, "label": key.label} if key else None,
            attachment_file_ids=event.attachment_file_ids,
        )

    after_commit(session, _defer)
    return notification_id


async def _load_attachments(session: AsyncSession, file_ids: list[int]) -> list[MailAttachment]:
    if not file_ids:
        return []
    files = (await session.scalars(select(StoredFile).where(StoredFile.id.in_(file_ids)))).all()
    return [MailAttachment(f.filename, f.content_type, read_file(f)) for f in files]


async def send_notification(
    job: JobContext,
    *,
    notification_id: str,
    template_id: str,
    to: list[str],
    cc: list[str],
    context: dict[str, Any],
    entity: dict[str, Any] | None = None,
    attachment_file_ids: list[int] | None = None,
) -> str:
    """Task body of ``notifications.send``; returns the SMTP Message-ID."""
    settings = get_settings()
    attempts = job.job.attempts if job.job is not None else 0
    with job_context(Trigger.JOB):
        try:
            rendered = render(template_id, context)
            async with open_session() as session:
                attachments = await _load_attachments(session, attachment_file_ids or [])
            headers = {"X-Hangar-Template": template_id, "X-Hangar-Notification": notification_id}
            if entity:
                headers["X-Hangar-Entity"] = f"{entity['type']}:{entity['id']}"
            sent = await gateways.mailer().send(
                OutgoingMail(
                    sender=settings.mail_from,
                    to=to,
                    cc=cc,
                    reply_to=settings.mail_team_mailbox,
                    subject=rendered.subject,
                    html=rendered.html,
                    text=rendered.text,
                    attachments=attachments,
                    headers=headers,
                )
            )
        except Exception as exc:
            log.warning(
                "notification_failed",
                notification_id=notification_id,
                attempt=attempts,
                error=str(exc),
            )
            if attempts >= len(RETRY_DELAYS):
                async with open_session() as session:
                    await emit(
                        session, NotificationFailed(notification_id, f"{type(exc).__name__}: {exc}")
                    )
                    await commit(session)
            raise
        async with open_session() as session:
            await emit(
                session,
                NotificationSent(
                    notification_id=notification_id,
                    subject=rendered.subject,
                    html=rendered.html,
                    text=rendered.text,
                    message_id=sent.message_id,
                    sent_at=datetime.now(UTC),
                ),
            )
            await commit(session)
    log.info("notification_sent", notification_id=notification_id, template=template_id)
    return sent.message_id


def register_notification_task() -> None:
    from app.jobs.app import register_task

    register_task(
        send_notification, name=TASK_NAME, queue="mail", retry=MailRetry(), pass_context=True
    )
