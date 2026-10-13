"""notify() pipeline, event bus phases, background jobs (SPEC §2.4, §8.5; #13, #14, #48)."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from procrastinate.testing import InMemoryConnector
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gateways
from app.core.context import current_actor_id
from app.core.db import commit
from app.core.enums import JobKind, JobStatus, Trigger
from app.core.events import EntityKey, NotificationQueued, NotificationSent, Transitioned, bus, emit
from app.core.gateways.mailers import MemoryMailer, SmtpMailer
from app.core.gateways.protocols import MailerError, OutgoingMail
from app.core.jobs import create_job, enqueue, tracked
from app.core.models import BackgroundJob
from app.core.notifications import notify, render, send_notification, templates
from app.testing import TEST_TEMPLATE

CONTEXT = dict(TEST_TEMPLATE.sample_context)


@pytest.fixture(autouse=True)
def template() -> Iterator[None]:
    templates.register(TEST_TEMPLATE)
    yield
    templates.unregister(TEST_TEMPLATE.id)


def test_render_uses_base_layout_and_escapes() -> None:
    rendered = render("test.notice", CONTEXT)
    assert rendered.subject == "Test notice for SR0012345"
    assert "Dear Ms Turner" in rendered.html
    assert "Acme IT Services" in rendered.html
    assert "On behalf of Globex Fleet AG" in rendered.html
    assert "&lt;b&gt;x&lt;/b&gt;" in rendered.html  # autoescaped
    assert "This is a demo system" in rendered.text
    assert "14 Oct 2026, 09:00" in rendered.text  # business TZ (Europe/Zurich, CEST)


async def test_notify_emits_queued_and_defers_after_commit(
    db_session: AsyncSession, jobs: InMemoryConnector
) -> None:
    seen: list[NotificationQueued] = []

    async def capture(session: AsyncSession, event: NotificationQueued) -> None:
        seen.append(event)

    bus.subscribe(NotificationQueued, capture, phase="in_transaction")
    try:
        token = current_actor_id.set(42)
        notification_id = await notify(
            db_session,
            "test.notice",
            to=["mia.turner@example.com"],
            context=CONTEXT,
            entity=EntityKey("service_request", 1, "SR0012345"),
            attachments=[7],
        )
        current_actor_id.reset(token)
        assert [e.notification_id for e in seen] == [notification_id]
        assert seen[0].actor_id == 42
        assert seen[0].trigger == Trigger.USER
        assert seen[0].attachment_file_ids == [7]
        assert jobs.jobs == {}  # nothing before commit
        await commit(db_session)
        [job] = jobs.jobs.values()
        assert job["task_name"] == "notifications.send"
        assert job["queue_name"] == "mail"
        assert job["args"]["notification_id"] == notification_id
    finally:
        bus.unsubscribe(NotificationQueued, capture)


async def test_send_task_uses_mailer_and_emits_sent(mailer: MemoryMailer) -> None:
    sent: list[NotificationSent] = []

    async def capture(session: AsyncSession, event: NotificationSent) -> None:
        sent.append(event)

    bus.subscribe(NotificationSent, capture, phase="in_transaction")
    try:
        message_id = await send_notification(
            SimpleNamespace(job=SimpleNamespace(attempts=0)),  # type: ignore[arg-type]
            notification_id="n-1",
            template_id="test.notice",
            to=["mia.turner@example.com"],
            cc=[],
            context=CONTEXT,
            entity={"type": "service_request", "id": 1, "label": "SR0012345"},
        )
    finally:
        bus.unsubscribe(NotificationSent, capture)
    [mail] = mailer.outbox
    assert mail.subject == "Test notice for SR0012345"
    assert mail.headers["X-Hangar-Template"] == "test.notice"
    assert mail.headers["X-Hangar-Entity"] == "service_request:1"
    assert mail.reply_to == "rollout-team@example.com"
    assert sent
    assert sent[0].message_id == message_id


async def test_demo_mode_refuses_foreign_recipients() -> None:
    with pytest.raises(MailerError):
        await MemoryMailer().send(
            OutgoingMail(
                sender="a@example.com",
                to=["someone@elsewhere.test"],
                cc=[],
                reply_to=None,
                subject="x",
                html="x",
                text="x",
            )
        )


def _mailpit_up() -> bool:
    try:
        with socket.create_connection(("localhost", 1025), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _mailpit_up(), reason="Mailpit not reachable on localhost:1025")
async def test_smtp_mailer_reaches_mailpit() -> None:
    restore = gateways.override(mailer=SmtpMailer())
    try:
        await send_notification(
            SimpleNamespace(job=SimpleNamespace(attempts=0)),  # type: ignore[arg-type]
            notification_id="mailpit-check",
            template_id="test.notice",
            to=["mailpit.check@example.com"],
            cc=[],
            context={**CONTEXT, "sr_number": "SR0099999"},
        )
    finally:
        restore()
    async with httpx.AsyncClient() as client:
        found = (
            await client.get(
                "http://localhost:8025/api/v1/search", params={"query": "subject:SR0099999"}
            )
        ).json()
        assert found["messages_count"] >= 1
        ids = [m["ID"] for m in found["messages"]]
        await client.request("DELETE", "http://localhost:8025/api/v1/messages", json={"IDs": ids})


async def test_event_phases(db_session: AsyncSession) -> None:
    calls: list[str] = []

    async def in_tx(session: AsyncSession, event: Any) -> None:
        calls.append("in_tx")

    async def after(event: Any) -> None:
        calls.append("after")

    bus.subscribe(Transitioned, in_tx, phase="in_transaction")
    bus.subscribe(Transitioned, after, phase="after_commit")
    try:
        event = Transitioned(
            "service_request", 1, "cancel", "imported", "cancelled", None, Trigger.USER
        )
        await emit(db_session, event)
        assert calls == ["in_tx"]
        await commit(db_session)
        assert calls == ["in_tx", "after"]
        await db_session.execute(text("SELECT 1"))  # handlers belong to a running transaction
        await emit(db_session, event)
        await db_session.rollback()  # after-commit handlers are discarded on rollback
        await commit(db_session)
        assert calls == ["in_tx", "after", "in_tx"]
    finally:
        bus.unsubscribe(Transitioned, in_tx)
        bus.unsubscribe(Transitioned, after)


async def test_enqueue_defers_with_job_id(
    db_session: AsyncSession, jobs: InMemoryConnector
) -> None:
    job = await enqueue(db_session, "demo.reset", kind=JobKind.DEMO_RESET, created_by=None)
    assert job.status == JobStatus.QUEUED
    await commit(db_session)
    [row] = jobs.jobs.values()
    assert (row["task_name"], row["args"]) == ("demo.reset", {"job_id": job.id})


async def test_tracked_job_progress(engine: Any) -> None:
    from app.core.db import open_session

    async with open_session() as session:
        job = await create_job(session, JobKind.STOCK_RECALCULATE)
        await session.commit()
        job_id = job.id
    async with tracked(job_id) as run:
        await run.progress(5, 10, "half way")
        run.result = {"kind": "stock_recalculate", "articles": 1}
    async with open_session() as session:
        stored = await session.get(BackgroundJob, job_id)
        assert stored is not None
        assert (stored.status, stored.progress_processed, stored.progress_total) == (
            JobStatus.SUCCEEDED,
            5,
            10,
        )
        assert stored.result == {"kind": "stock_recalculate", "articles": 1}
        await session.delete(stored)
        await session.commit()
