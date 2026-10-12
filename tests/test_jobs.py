"""Job registration, the 202 -> GET /jobs/{id} flow and commit hooks."""

from collections.abc import Callable

from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import commit, on_commit
from app.core.schemas import JobAccepted
from app.jobs import system
from app.jobs.app import job_app


def test_system_tasks_are_registered_with_namespace() -> None:
    assert {"system:noop", "system:cleanup_jobs"} <= set(job_app.tasks)
    periodic = {t.task.name for t in job_app.periodic_registry.periodic_tasks.values()}
    assert "system:cleanup_jobs" in periodic


async def test_job_status_endpoint(
    client: AsyncClient, jobs: InMemoryConnector, auth_headers: dict[str, str]
) -> None:
    job_id = await system.noop.defer_async(message="hello")
    accepted = JobAccepted.for_job(job_id)
    assert accepted.model_dump() == {"jobId": job_id, "statusUrl": f"/api/v1/jobs/{job_id}"}

    response = await client.get(accepted.status_url, headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {
        "id": job_id,
        "task": "system:noop",
        "queue": "default",
        "status": "todo",
        "attempts": 0,
        "scheduledAt": None,
        "finished": False,
    }

    await job_app.run_worker_async(wait=False, install_signal_handlers=False)
    done = await client.get(accepted.status_url, headers=auth_headers)
    assert done.json()["status"] == "succeeded"
    assert done.json()["finished"] is True


async def test_job_status_requires_auth_and_existing_job(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    assert (await client.get("/api/v1/jobs/1")).status_code == 401
    missing = await client.get("/api/v1/jobs/999999", headers=auth_headers)
    assert missing.status_code == 404


async def test_on_commit_defers_only_after_commit(
    db_session: AsyncSession, jobs: InMemoryConnector
) -> None:
    calls: list[str] = []

    async def defer() -> None:
        calls.append("deferred")
        await system.noop.defer_async(message="after-commit")

    on_commit(db_session, defer)
    assert calls == []
    await commit(db_session)
    assert calls == ["deferred"]
    assert [job["task_name"] for job in jobs.jobs.values()] == ["system:noop"]

    await db_session.execute(text("SELECT 1"))  # hooks belong to a running transaction
    on_commit(db_session, defer)
    await db_session.rollback()
    await commit(db_session)
    assert calls == ["deferred"]  # discarded by the rollback


def test_make_token_fixture(make_token: Callable[..., str]) -> None:
    assert make_token().count(".") == 2
