"""H2 seed story (SPEC §11.7) and samples (SPEC §7.9)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ImportJobStatus, ImportType
from app.core.models import BackgroundJob, StoredFile
from app.core.seed.framework import SeedContext
from app.modules.h2_imports.importing.reader import read_table
from app.modules.h2_imports.importing.validate import validate_table
from app.modules.h2_imports.models import ImportJob, ImportJobError
from app.modules.h2_imports.samples import SAMPLE_FILES, build_sample
from app.modules.h2_imports.seed import seed_imports, simulate_issues


async def test_seeded_import_jobs(db_session: AsyncSession) -> None:
    jobs = (await db_session.scalars(select(ImportJob).order_by(ImportJob.created_at))).all()
    assert [(j.type, j.status) for j in jobs] == [
        (ImportType.SERVICE_REQUESTS, ImportJobStatus.COMPLETED),
        (ImportType.LEASE_RETURNS, ImportJobStatus.COMPLETED),
        (ImportType.CONTACTS, ImportJobStatus.COMPLETED),
        (ImportType.ASSETS_NEW, ImportJobStatus.COMPLETED),
        (ImportType.ASSET_STATUS_SYNC, ImportJobStatus.COMPLETED),
        (ImportType.SERVICE_REQUESTS, ImportJobStatus.COMPLETED_WITH_ERRORS),
        (ImportType.ASSETS_NEW, ImportJobStatus.CANCELLED),
    ]
    with_errors = jobs[5]
    assert (with_errors.error_count, with_errors.warning_count) == (4, 3)
    codes = (
        await db_session.scalars(
            select(ImportJobError.code)
            .where(ImportJobError.import_job_id == with_errors.id)
            .order_by(ImportJobError.row_number)
        )
    ).all()
    assert sorted(codes) == sorted(
        [
            "email_missing",
            "invalid_format",
            "packed_missing_key",
            "packed_duplicate_key",
            "unknown_article",
            "packed_unknown_key",
            "duplicate_in_file",
        ]
    )
    assert with_errors.error_report_file_id is not None
    assert jobs[1].options["markMissingAsProbablyClosed"] is True
    assert jobs[1].post_process["probablyClosed"] == 6
    assert jobs[3].warning_count == 2  # two unknown models
    assert jobs[4].skipped_count == 1
    assert jobs[4].warning_count == 1
    for job in jobs:
        background = await db_session.get(BackgroundJob, job.background_job_id)
        assert background is not None
        assert background.entity_id == job.id
        upload = await db_session.get(StoredFile, job.file_id)
        assert upload is not None
        assert upload.purpose == "import_upload"
    # idempotent
    await seed_imports(db_session, SeedContext.create(20))
    assert await db_session.scalar(select(func.count()).select_from(ImportJob)) == 7


async def test_samples_match_the_story(db_session: AsyncSession) -> None:
    expected_rows = {
        "lease_returns": 40,
        "service_requests": 30,
        "service_requests_with_errors": 25,
        "contacts": 30,
        "assets_new": 60,
        "asset_status_sync": 20,
        "deactivation": 25,
    }
    for name, import_type in SAMPLE_FILES.items():
        data = await build_sample(db_session, name)
        table = read_table(data, ".csv")
        assert table.row_count == expected_rows[name], name
        if import_type is None:
            continue
        validation = validate_table(table, import_type)
        await simulate_issues(db_session, validation)
        errors = sum(1 for r in validation.rows if r.has_errors)
        warnings = sum(1 for r in validation.rows if r.has_warnings)
        expected = {
            "service_requests_with_errors": (4, 3),
            "assets_new": (0, 2),
            "asset_status_sync": (0, 1),
        }.get(name, (0, 0))
        assert (errors, warnings) == expected, name
