"""The single procrastinate ``App``.

Tasks are defined on per-module blueprints (``app/modules/<name>/jobs.py`` exporting
``blueprint``) and registered here under the namespace ``<name>``, e.g.
``imports_master:run_import``. Defer from services with the task object::

    from app.modules.imports_master.jobs import run_import
    job_id = await run_import.defer_async(import_id=batch.id)
"""

from __future__ import annotations

import procrastinate
from procrastinate import Blueprint, PsycopgConnector

from app.core.config import get_settings
from app.core.discovery import discover
from app.jobs import system


def _connector() -> PsycopgConnector:
    settings = get_settings()
    return PsycopgConnector(conninfo=settings.psycopg_conninfo, min_size=1, max_size=10)


def build_job_app() -> procrastinate.App:
    settings = get_settings()
    app = procrastinate.App(
        connector=_connector(),
        worker_defaults={"concurrency": settings.worker_concurrency},
    )
    app.add_tasks_from(system.blueprint, namespace="system")
    for module, imported in discover("jobs"):
        blueprint = getattr(imported, "blueprint", None)
        if not isinstance(blueprint, Blueprint):
            raise TypeError(
                f"app.modules.{module}.jobs must export `blueprint: procrastinate.Blueprint`"
            )
        app.add_tasks_from(blueprint, namespace=module)
    return app


job_app = build_job_app()
