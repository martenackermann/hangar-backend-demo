"""The single procrastinate ``App`` (SPEC §9).

Tasks are added by core modules and by slices through ``SliceRegistry.register_task``
(which calls :func:`register_task`). Task names are dotted (``imports.process``). The API
defers via ``job_app.configure_task(name).defer_async(**kwargs)`` or the task object.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import procrastinate
from procrastinate import PsycopgConnector

from app.core.config import get_settings

QUEUES = ("default", "imports", "mail", "sync")


def _build() -> procrastinate.App:
    settings = get_settings()
    return procrastinate.App(
        connector=PsycopgConnector(conninfo=settings.psycopg_conninfo, min_size=1, max_size=10),
        worker_defaults={"concurrency": settings.worker_concurrency},
    )


job_app = _build()


def register_task(
    fn: Callable[..., Any],
    *,
    name: str,
    queue: str = "default",
    lock: str | None = None,
    queueing_lock: str | None = None,
    retry: Any = None,
    pass_context: bool = False,
    periodic: str | None = None,
    periodic_id: str = "",
) -> Any:
    """Register ``fn`` as task ``name``; ``periodic`` is a cron expression (UTC)."""
    if name in job_app.tasks:
        return job_app.tasks[name]
    options: dict[str, Any] = {"name": name, "queue": queue, "pass_context": pass_context}
    if lock is not None:
        options["lock"] = lock
    if queueing_lock is not None:
        options["queueing_lock"] = queueing_lock
    if retry is not None:
        options["retry"] = retry
    task = job_app.task(**options)(fn)
    if periodic is not None:
        job_app.periodic(cron=periodic, periodic_id=periodic_id)(task)
    return task


async def defer(task_name: str, **kwargs: Any) -> int:
    """Defer a registered (or known-by-name) task; returns the procrastinate job id."""
    return await job_app.configure_task(task_name).defer_async(**kwargs)
