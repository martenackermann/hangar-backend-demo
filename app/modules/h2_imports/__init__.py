"""H2 Import & Master data slice (see README.md)."""

from __future__ import annotations

from pathlib import Path

from app.core.slices import SliceRegistry

TEMPLATES = Path(__file__).parent / "templates"


def register(reg: SliceRegistry) -> None:
    from app.core.demo import table_exporter
    from app.core.notifications import NotificationTemplate

    from . import api, seed, tasks

    for router in api.ROUTERS:
        reg.include_router(router)

    reg.register_template(
        NotificationTemplate(
            id="import.summary",
            owner="h2",
            name="Import summary",
            subject="Import #{{ import_id }} ({{ import_type }}) finished with {{ error_count }} errors",
            html=TEMPLATES / "import.summary.html.j2",
            text=TEMPLATES / "import.summary.txt.j2",
            description="Sent to the uploader when an import finished with row errors; "
            "the failed-row report is attached.",
            trigger="import finished with errors",
            recipients="Uploader",
            entity_type="import_job",
            sample_context={
                "import_id": 57,
                "import_type": "service_requests",
                "type_label": "Service requests",
                "status_label": "Imported with issues",
                "file_name": "itsm_sr_export.csv",
                "first_name": "Dana",
                "total": 25,
                "created": 21,
                "updated": 0,
                "unchanged": 0,
                "skipped": 0,
                "error_count": 4,
                "warning_count": 3,
                "detail_url": "http://localhost:5173/data/imports/57",
                "has_report": True,
            },
        )
    )

    # Jobs (SPEC §9): chunked import processing and region re-resolution
    reg.register_task(tasks.process_import, name="imports.process", queue="imports")
    reg.register_task(
        tasks.reapply_regions_task,
        name="regions.reapply",
        queue="default",
        lock="regions.reapply",
        queueing_lock="regions.reapply",
    )

    # Seed (order 20), seed.json export and demo truncation
    reg.register_seeder(20, seed.seed_imports)
    reg.register_seed_exporter(table_exporter("import_jobs", "import_job_errors"))
    reg.register_demo_truncate(["import_job_errors", "import_jobs"])
