"""``GET /api/v1/health`` — liveness/readiness for the API, database and job queue."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Response
from sqlalchemy import text

from app import __version__
from app.core.config import get_settings
from app.core.db import SessionDep
from app.core.schemas import ApiModel

router = APIRouter(tags=["system"])

type CheckStatus = Literal["ok", "degraded", "error"]

#: A worker counts as alive if it sent a heartbeat within this window.
WORKER_HEARTBEAT_WINDOW_SECONDS = 60


class DatabaseCheck(ApiModel):
    status: CheckStatus
    latency_ms: float | None = None
    detail: str | None = None


class JobQueueCheck(ApiModel):
    status: CheckStatus
    pending: int | None = None
    running: int | None = None
    failed: int | None = None
    workers: int | None = None
    detail: str | None = None


class HealthChecks(ApiModel):
    database: DatabaseCheck
    job_queue: JobQueueCheck


class HealthOut(ApiModel):
    status: CheckStatus
    version: str
    environment: str
    demo_mode: bool
    checks: HealthChecks


_QUEUE_SQL = text(
    """
    SELECT
      (SELECT count(*) FROM procrastinate_jobs WHERE status = 'todo') AS pending,
      (SELECT count(*) FROM procrastinate_jobs WHERE status = 'doing') AS running,
      (SELECT count(*) FROM procrastinate_jobs WHERE status = 'failed') AS failed,
      (SELECT count(*) FROM procrastinate_workers
         WHERE last_heartbeat > now() - make_interval(secs => :window)) AS workers
    """
)


@router.get(
    "/health",
    response_model=HealthOut,
    summary="Service health",
    responses={503: {"model": HealthOut, "description": "A dependency is unavailable"}},
)
async def health(session: SessionDep, response: Response) -> HealthOut:
    settings = get_settings()

    started = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
        database = DatabaseCheck(
            status="ok", latency_ms=round((time.perf_counter() - started) * 1000, 1)
        )
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        await session.rollback()
        database = DatabaseCheck(status="error", detail=type(exc).__name__)

    if database.status != "ok":
        queue = JobQueueCheck(status="error", detail="database unavailable")
    else:
        exists = (
            await session.execute(text("SELECT to_regclass('procrastinate_jobs') IS NOT NULL"))
        ).scalar_one()
        if not exists:
            queue = JobQueueCheck(
                status="error", detail="job schema missing (run `hangar jobs-schema`)"
            )
        else:
            row = (
                await session.execute(_QUEUE_SQL, {"window": WORKER_HEARTBEAT_WINDOW_SECONDS})
            ).one()
            queue = JobQueueCheck(
                status="ok" if row.workers else "degraded",
                pending=row.pending,
                running=row.running,
                failed=row.failed,
                workers=row.workers,
                detail=None if row.workers else "no active worker",
            )

    if database.status == "error" or queue.status == "error":
        overall: CheckStatus = "error"
        response.status_code = 503
    elif queue.status == "degraded":
        overall = "degraded"
    else:
        overall = "ok"

    return HealthOut(
        status=overall,
        version=__version__,
        environment=settings.environment,
        demo_mode=settings.demo_mode,
        checks=HealthChecks(database=database, job_queue=queue),
    )
