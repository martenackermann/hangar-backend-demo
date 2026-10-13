"""Shared helpers for the H5 tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector

DANA = "dana.dispatcher@example.com"
THEO = "theo.technician@example.com"
VIC = "vic.viewer@example.com"
ADA = "ada.admin@example.com"
TARA = "tara.lindqvist@example.com"

type Headers = Callable[[str], Awaitable[dict[str, str]]]


def queued_mails(jobs: InMemoryConnector, template_id: str | None = None) -> list[dict[str, Any]]:
    return [
        job["args"]
        for job in jobs.jobs.values()
        if job["task_name"] == "notifications.send"
        and (template_id is None or job["args"]["template_id"] == template_id)
    ]


def deferred(jobs: InMemoryConnector, task_name: str) -> list[dict[str, Any]]:
    return [job for job in jobs.jobs.values() if job["task_name"] == task_name]


async def deliver(args: dict[str, Any]) -> str:
    """Run ``notifications.send`` for one queued mail (attachments skipped: the task's own
    session cannot see the test transaction)."""
    from app.core.notifications import send_notification

    return await send_notification(
        SimpleNamespace(job=SimpleNamespace(attempts=0)),  # type: ignore[arg-type]
        **{**args, "attachment_file_ids": []},
    )


async def get_json(client: AsyncClient, url: str, auth: dict[str, str], **params: Any) -> Any:
    response = await client.get(url, headers=auth, params=params)
    assert response.status_code == 200, response.text
    return response.json()
