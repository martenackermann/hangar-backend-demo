"""H2 seed (order 20, SPEC §11.7): seven historical import jobs by Dana with stored uploads.

The uploads are the sample files of :mod:`.samples`; validation issues are computed with the
real parser plus the catalogue/contact checks the appliers would make, so the issue tables
look exactly like a real run. Counts are story numbers (the jobs are history — the core seed
already contains the resulting data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ImportJobStatus, ImportType, JobKind, JobStatus
from app.core.files import store_file
from app.core.jobs import record_job
from app.core.models import Article, Contact
from app.core.seed.framework import SeedContext

from .importing.appliers.status_sync import map_state
from .importing.issues import Issue
from .importing.reader import read_table
from .importing.report import build_error_report
from .importing.validate import Validation, validate_table
from .models import ImportJob, ImportJobError, empty_post_process
from .samples import build_sample

DANA = "dana.dispatcher@example.com"


@dataclass(slots=True)
class SeedJob:
    sample: str
    type: ImportType
    days_ago: int
    hour: int
    status: ImportJobStatus
    filename: str
    options: dict[str, Any] = field(default_factory=dict)
    post: dict[str, int] = field(default_factory=dict)
    updated: int = 0
    skipped: int = 0


def _story(ctx: SeedContext) -> list[SeedJob]:
    day = ctx.today
    return [
        SeedJob(
            "service_requests",
            ImportType.SERVICE_REQUESTS,
            9,
            8,
            ImportJobStatus.COMPLETED,
            f"itsm_sr_export_{day - timedelta(days=9):%Y%m%d}.csv",
            {"dryRun": False},
            {
                "leaseReturnsLinked": 3,
                "hardwareHold": 2,
                "contactsCreated": 21,
                "contactsUpdated": 9,
            },
        ),
        SeedJob(
            "lease_returns",
            ImportType.LEASE_RETURNS,
            8,
            9,
            ImportJobStatus.COMPLETED,
            f"itsm_lease_export_{day - timedelta(days=8):%Y%m%d}.csv",
            {"dryRun": False, "markMissingAsProbablyClosed": True},
            {"probablyClosed": 6, "contactsCreated": 28, "contactsUpdated": 7},
            updated=5,
        ),
        SeedJob(
            "contacts",
            ImportType.CONTACTS,
            7,
            10,
            ImportJobStatus.COMPLETED,
            f"itsm_users_{day - timedelta(days=7):%Y%m%d}.csv",
            {"dryRun": False},
            {"contactsCreated": 30},
        ),
        SeedJob(
            "assets_new",
            ImportType.ASSETS_NEW,
            5,
            11,
            ImportJobStatus.COMPLETED,
            f"initech_delivery_{day - timedelta(days=5):%Y%m%d}.csv",
            {"dryRun": False, "targetLocationId": "RIV-GOODS_IN"},
        ),
        SeedJob(
            "asset_status_sync",
            ImportType.ASSET_STATUS_SYNC,
            4,
            8,
            ImportJobStatus.COMPLETED,
            f"itsm_install_states_{day - timedelta(days=4):%Y%m%d}.csv",
            {"dryRun": False},
            updated=6,
            skipped=1,
        ),
        SeedJob(
            "service_requests_with_errors",
            ImportType.SERVICE_REQUESTS,
            1,
            9,
            ImportJobStatus.COMPLETED_WITH_ERRORS,
            f"itsm_sr_export_{day - timedelta(days=1):%Y%m%d}.csv",
            {"dryRun": False},
            {"contactsCreated": 9, "contactsUpdated": 6},
        ),
        SeedJob(
            "assets_new",
            ImportType.ASSETS_NEW,
            1,
            14,
            ImportJobStatus.CANCELLED,
            f"initech_delivery_{day - timedelta(days=1):%Y%m%d}.csv",
            {"dryRun": False, "targetLocationId": "EAS-GOODS_IN"},
        ),
    ]


async def simulate_issues(session: AsyncSession, validation: Validation) -> None:
    """Add the DB-dependent warnings the appliers would raise (catalogue, contacts, states)."""
    types = {
        t.lower()
        for t in (
            await session.scalars(
                select(func.lower(Article.internal_description)).where(Article.is_active)
            )
        ).all()
    }
    models = {m.lower() for m in (await session.scalars(select(Article.model_number))).all()}
    with_email = set(
        (await session.scalars(select(Contact.user_id).where(Contact.email.is_not(None)))).all()
    )
    for row in validation.rows:
        if row.has_errors:
            continue
        values = row.values
        if validation.type == ImportType.SERVICE_REQUESTS:
            details = values.get("order_details") or {}
            for key, label in (
                ("new_base", "New base device type"),
                ("new_peripheral_1", "New peripheral 1 type"),
                ("new_peripheral_2", "New peripheral 2 type"),
            ):
                requested = details.get(key)
                if requested and requested.lower() not in types:
                    row.issues.append(
                        Issue.warning(
                            "unknown_article",
                            f"Device type '{requested}' is not in the article catalogue.",
                            f"order_details.{label}",
                            requested,
                        )
                    )
            if values.get("email") is None and values.get("user_id") not in with_email:
                row.issues.append(
                    Issue.warning(
                        "email_missing",
                        "The contact has no email address; automatic mails cannot be sent.",
                        "email",
                    )
                )
        elif validation.type in (ImportType.ASSETS_NEW, ImportType.ASSET_STATUS_SYNC):
            model = values.get("model")
            if model and model.lower() not in models:
                row.issues.append(
                    Issue.warning(
                        "unknown_model",
                        f"Model '{model}' is not in the article catalogue; the asset has no article.",
                        "model",
                        model,
                    )
                )
            if validation.type == ImportType.ASSET_STATUS_SYNC and (
                map_state(values.get("install_state"), values.get("stockroom")) is None
            ):
                row.issues.append(
                    Issue.warning(
                        "unmapped_state",
                        f"Install state '{values.get('install_state')}' is not synchronised; "
                        "the row was skipped.",
                        "install_state",
                        values.get("install_state"),
                    )
                )


async def seed_imports(session: AsyncSession, ctx: SeedContext) -> None:
    if await session.scalar(select(ImportJob.id).limit(1)) is not None:
        return
    dana = await ctx.user(session, DANA)
    for spec in _story(ctx):
        data = await build_sample(session, spec.sample)
        validation = validate_table(read_table(data, ".csv"), spec.type)
        await simulate_issues(session, validation)
        rows = validation.rows
        total = len(rows)
        errors = sum(1 for r in rows if r.has_errors)
        warnings = sum(1 for r in rows if r.has_warnings)
        created_at = ctx.at(ctx.today - timedelta(days=spec.days_ago), spec.hour, 12)
        cancelled = spec.status == ImportJobStatus.CANCELLED
        started = None if cancelled else created_at + timedelta(seconds=4)
        finished = created_at + timedelta(seconds=(30 if cancelled else 4 + max(2, total // 12)))
        options = dict(spec.options)
        if isinstance(options.get("targetLocationId"), str):
            options["targetLocationId"] = (
                await ctx.location(session, options["targetLocationId"])
            ).id
        processed = 0 if cancelled else total
        applied = processed - errors if not cancelled else 0
        counts = {
            "created": max(0, applied - spec.updated - spec.skipped),
            "updated": spec.updated if not cancelled else 0,
            "unchanged": 0,
            "skipped": spec.skipped if not cancelled else 0,
            "errors": errors if not cancelled else 0,
            "warnings": warnings if not cancelled else 0,
        }
        job_status = JobStatus.CANCELLED if cancelled else JobStatus.SUCCEEDED
        background = await record_job(
            session,
            JobKind.IMPORT,
            job_status,
            created_by=dana,
            message="Cancelled" if cancelled else f"Import {spec.status}",
            progress=(processed, total),
            created_at=created_at,
            started_at=started,
            finished_at=finished,
            cancel_requested=cancelled,
        )
        stored = await store_file(
            session,
            data,
            spec.filename,
            "text/csv",
            "import_upload",
            created_by=dana,
            created_at=created_at,
        )
        job = ImportJob(
            background_job_id=background.id,
            type=spec.type,
            type_detected=True,
            status=spec.status,
            file_id=stored.id,
            original_filename=spec.filename,
            options=options,
            total_rows=total,
            processed_rows=processed,
            created_count=counts["created"],
            updated_count=counts["updated"],
            unchanged_count=counts["unchanged"],
            skipped_count=counts["skipped"],
            error_count=counts["errors"],
            warning_count=counts["warnings"],
            post_process={**empty_post_process(), **(spec.post if not cancelled else {})},
            job_warnings=validation.job_warnings,
            cancel_requested=cancelled,
            created_by=dana.id,
            created_at=created_at,
            updated_at=finished,
            started_at=started,
            finished_at=finished,
        )
        session.add(job)
        await session.flush()
        stored.entity_type, stored.entity_id = "import_job", job.id
        background.entity_type, background.entity_id = "import_job", job.id
        background.result = {
            "kind": "import",
            "importJobId": job.id,
            "status": spec.status.value,
            "counts": {"total": total, "processed": processed, **counts},
        }
        if cancelled:
            continue
        issues = [(row.number, issue) for row in rows for issue in row.issues]
        if not issues:
            continue
        for row in rows:
            for issue in row.issues:
                session.add(
                    ImportJobError(
                        import_job_id=job.id,
                        row_number=row.number,
                        severity=issue.severity,
                        column_name=issue.column,
                        code=issue.code,
                        message=issue.message,
                        raw_value=issue.raw_value,
                        business_key=(row.key or "")[:40] or None,
                        created_at=finished,
                    )
                )
        report = await store_file(
            session,
            build_error_report(validation.original_headers, validation.original, issues),
            f"import-{job.id}-issues.csv",
            "text/csv",
            "import_error_report",
            entity=("import_job", job.id),
            created_by=dana,
            created_at=finished,
        )
        job.error_report_file_id = report.id
    await session.flush()
