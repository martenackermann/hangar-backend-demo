"""Core helpers requested by the slices at integration time: savepoints that drop their
after-commit callbacks, job cancel/record helpers, file-reference-aware cleanup and the
per-hub article settings."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.bootstrap import file_reference_columns
from app.core.db import after_commit, savepoint
from app.core.enums import ArticleCategory, JobKind, JobStatus, SiteKind
from app.core.files import store_file
from app.core.jobs import create_job, record_job, request_cancel
from app.core.models import Article, ArticleSiteSetting, BackgroundJob, Site
from app.core.stock_math import ensure_article_site_settings
from app.core.timeutil import now_utc
from app.testing import factories as f

PENDING = "hangar.after_commit"


async def _noop() -> None:
    return None


async def test_savepoint_keeps_callbacks_on_success(db_session: AsyncSession) -> None:
    before = len(db_session.info.get(PENDING, []))
    async with savepoint(db_session):
        after_commit(db_session, _noop)
    assert len(db_session.info[PENDING]) == before + 1
    db_session.info[PENDING].pop()


async def test_savepoint_drops_callbacks_and_rows_on_error(db_session: AsyncSession) -> None:
    dana = await f.user(db_session)
    before = len(db_session.info.get(PENDING, []))
    created: list[int] = []

    async def failing_row() -> None:
        async with savepoint(db_session):
            job = await create_job(db_session, JobKind.IMPORT, dana)
            created.append(job.id)
            after_commit(db_session, _noop)
            raise RuntimeError("row failed")

    with pytest.raises(RuntimeError):
        await failing_row()
    job_id = created[0]
    assert len(db_session.info.get(PENDING, [])) == before
    db_session.expunge_all()
    assert await db_session.get(BackgroundJob, job_id) is None


async def test_request_cancel_only_for_unfinished_jobs(db_session: AsyncSession) -> None:
    dana = await f.user(db_session)
    queued = await create_job(db_session, JobKind.IMPORT, dana)
    done = await record_job(db_session, JobKind.IMPORT, JobStatus.SUCCEEDED, created_by=dana)
    assert await request_cancel(db_session, queued.id) is True
    assert await request_cancel(db_session, done.id) is False
    assert await request_cancel(db_session, 999_999_999) is False
    await db_session.refresh(queued)
    assert queued.cancel_requested is True


async def test_record_job_backdates_history(db_session: AsyncSession) -> None:
    created = now_utc() - timedelta(days=3)
    job = await record_job(
        db_session,
        JobKind.IMPORT,
        JobStatus.CANCELLED,
        entity=("import_job", 1),
        progress=(0, 40),
        created_at=created,
        started_at=None,
        finished_at=created + timedelta(seconds=30),
        cancel_requested=True,
        result={"kind": "import"},
    )
    await db_session.refresh(job)
    assert job.created_at == created
    assert job.updated_at == created + timedelta(seconds=30)
    assert (job.status, job.progress_total, job.entity_type) == (
        JobStatus.CANCELLED,
        40,
        "import_job",
    )
    stored = await store_file(db_session, b"a,b\n", "x.csv", "text/csv", "test", created_at=created)
    assert stored.created_at == created


def test_file_cleanup_knows_every_reference() -> None:
    columns = {f"{c.table.name}.{c.name}" for c in file_reference_columns()}
    assert "service_requests.protocol_file_id" in columns
    # slice tables are found through the shared metadata (no slice names in core code)
    assert {"import_jobs.file_id", "import_jobs.error_report_file_id"} <= columns
    assert "shipments.label_file_id" in columns


async def test_ensure_article_site_settings(db_session: AsyncSession) -> None:
    article = Article(
        number="T-0001",
        category=ArticleCategory.LAPTOP,
        name="Test laptop",
        model_number="TEST-0001",
        manufacturer="Acme",
        internal_description="Test laptop 13",
    )
    db_session.add(article)
    await db_session.flush()
    hubs = await db_session.scalar(
        select(func.count()).select_from(Site).where(Site.kind == SiteKind.HUB)
    )
    assert await ensure_article_site_settings(db_session, article.id) == hubs
    assert await ensure_article_site_settings(db_session, article.id) == 0
    count = await db_session.scalar(
        select(func.count())
        .select_from(ArticleSiteSetting)
        .where(ArticleSiteSetting.article_id == article.id)
    )
    assert count == hubs
