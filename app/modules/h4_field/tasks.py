"""Task ``appointments.sync`` (SPEC §9: queue ``sync``, hourly at :05, lock).

Manual runs (``POST /appointments/sync``) pass ``job_id``; periodic runs get ``timestamp``
from procrastinate and create their own ``background_jobs`` row.
"""

from __future__ import annotations

from app.core import gateways
from app.core.db import commit, open_session
from app.core.enums import JobKind, Trigger
from app.core.jobs import create_job, tracked

from .appointments import run_sync

TASK_NAME = "appointments.sync"
PERIODIC_CRON = "5 * * * *"


async def sync_appointments_task(timestamp: int | None = None, job_id: int | None = None) -> None:
    _ = timestamp
    if job_id is None:
        async with open_session() as session:
            job = await create_job(session, JobKind.APPOINTMENTS_SYNC)
            job_id = job.id
            await commit(session)
    async with tracked(job_id, trigger=Trigger.SYNC) as run:
        async with open_session() as session:
            counts = await run_sync(session, gateways.booking(), job_id=job_id)
            await commit(session)
        result = counts.model_dump(mode="json", by_alias=True)
        run.result = {"kind": "appointments_sync", **result}
        run.message = (
            f"{result['created']} new, {result['updated']} changed, "
            f"{result['cancelled']} cancelled, {result['transitions']} status changes"
        )
