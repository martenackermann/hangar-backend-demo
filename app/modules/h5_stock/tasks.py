"""``stock.recalculate`` (SPEC §9): hourly at :20 UTC, on demand, and debounced after
imports / deactivation / inventory close. Every run has a ``background_jobs`` row: the
request creates it for manual runs; periodic and debounced runs create their own."""

from __future__ import annotations

from app.core.db import commit, open_session
from app.core.enums import JobKind
from app.core.jobs import create_job, tracked

from .services.stock import run_recalculation


async def recalculate(job_id: int | None = None, timestamp: int | None = None) -> None:
    if job_id is None:
        async with open_session() as session:
            job = await create_job(
                session,
                JobKind.STOCK_RECALCULATE,
                message="Scheduled" if timestamp is not None else "After data change",
            )
            job_id = job.id
            await commit(session)
    async with tracked(job_id) as run:
        await run.progress(0, None, "Recalculating stock and demand")
        async with open_session() as session:
            run.result = await run_recalculation(session)
            await commit(session)
        run.message = "Stock and demand recalculated"
