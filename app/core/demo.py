"""Demo reset (SPEC §9 ``demo.reset``) and seed JSON export (SPEC_RESOLUTIONS #16)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import orjson
import structlog
from pydantic.alias_generators import to_camel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import open_session
from app.core.enums import JobKind, JobStatus
from app.core.jobs import public_error
from app.core.models import BackgroundJob
from app.core.seed import framework
from app.core.timeutil import business_hour, business_today, now_utc

log = structlog.get_logger(__name__)

SEED_JSON_VERSION = 1
NIGHTLY_HOUR = 2  # 02:30 business time (task fires hourly at minute 30)


# --- reset --------------------------------------------------------------------------------------


async def reset_and_seed(
    session: AsyncSession, *, keep_job: BackgroundJob | None = None
) -> datetime:
    """Truncate demo data, re-run the seed pipeline, restore ``keep_job``'s row (no commit)."""
    snapshot = None
    if keep_job is not None:
        snapshot = (
            (
                await session.execute(
                    text("SELECT * FROM background_jobs WHERE id = :id"), {"id": keep_job.id}
                )
            )
            .mappings()
            .one()
        )
    await framework.truncate_demo_data(session)
    if snapshot is not None:
        # seeded jobs must not take the id the client is polling
        await session.execute(
            text("SELECT setval(pg_get_serial_sequence('background_jobs', 'id'), :id)"),
            {"id": snapshot["id"]},
        )
    session.expunge_all()
    seeded_at = now_utc()
    await framework.run_seeders(session, seeded_at)
    if snapshot is not None:
        values = dict(snapshot)
        values["created_by"] = None  # the creator may not exist any more (non-persona user)
        values["status"] = JobStatus.SUCCEEDED.value
        values["finished_at"] = now_utc()
        values["result"] = orjson.dumps(
            {"kind": "demo_reset", "seededAt": seeded_at.isoformat()}
        ).decode()
        columns = ", ".join(values)
        params = ", ".join(f"CAST(:{k} AS jsonb)" if k == "result" else f":{k}" for k in values)
        await session.execute(
            text(
                f"INSERT INTO background_jobs ({columns}) OVERRIDING SYSTEM VALUE VALUES ({params})"
            ),
            values,
        )
    return seeded_at


async def run_demo_reset(job_id: int | None = None) -> None:
    """Task body of ``demo.reset``."""
    async with open_session() as session:
        job = await session.get(BackgroundJob, job_id) if job_id is not None else None
        if job is None:
            job = BackgroundJob(
                kind=JobKind.DEMO_RESET, status=JobStatus.RUNNING, started_at=now_utc()
            )
            session.add(job)
            await session.flush()
        else:
            job.status = JobStatus.RUNNING
            job.started_at = now_utc()
            await session.flush()
        try:
            await reset_and_seed(session, keep_job=job)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            job = await session.get(BackgroundJob, job.id)
            if job is not None:
                job.status = JobStatus.FAILED
                job.error = public_error(exc)
                job.finished_at = now_utc()
                await session.commit()
            raise
    log.info("demo_reset_done", job_id=job_id)


def demo_reset_allowed() -> bool:
    """Demo data may be (re)loaded: always outside production, in production only in demo
    mode (the demo personas share the published password ``demo1234``)."""
    settings = get_settings()
    return settings.demo_mode or not settings.is_production


async def demo_reset_task(job_id: int | None = None) -> None:
    if not get_settings().demo_mode:  # the endpoint is demo-only; a stale queued job is not
        log.warning("demo_reset_skipped", job_id=job_id, reason="demo mode is off")
        return
    await run_demo_reset(job_id)


async def demo_reset_nightly(timestamp: int) -> None:
    """Hourly at :30 UTC; proceeds at 02:xx business time once per day (DST-safe, SPEC §9)."""
    settings = get_settings()
    if not settings.demo_mode or not settings.demo_nightly_reset or business_hour() != NIGHTLY_HOUR:
        return
    async with open_session() as session:
        today = business_today()
        done = (
            await session.scalars(
                select(BackgroundJob)
                .where(
                    BackgroundJob.kind == JobKind.DEMO_RESET,
                    BackgroundJob.status == JobStatus.SUCCEEDED,
                )
                .order_by(BackgroundJob.id.desc())
            )
        ).first()
        if (
            done is not None
            and done.finished_at is not None
            and business_today(done.finished_at) == today
        ):
            return
    await run_demo_reset(None)


# --- seed JSON export ------------------------------------------------------------------------------


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


EXCLUDED_COLUMNS = {"users": {"password_hash"}}


async def table_rows(
    session: AsyncSession, table: str, *, order_by: str = "id"
) -> list[dict[str, Any]]:
    """Raw rows of ``table`` as camelCase JSON-friendly dicts."""
    excluded = EXCLUDED_COLUMNS.get(table, set())
    result = await session.execute(text(f"SELECT * FROM {table} ORDER BY {order_by}"))
    return [
        {to_camel(key): _json_value(value) for key, value in row.items() if key not in excluded}
        for row in result.mappings().all()
    ]


def table_exporter(*tables: str) -> framework.SeedExporter:
    """Exporter that dumps ``tables`` raw (``reg.register_seed_exporter(table_exporter("mail_log"))``)."""

    async def export(
        session: AsyncSession, ctx: framework.SeedContext
    ) -> dict[str, list[dict[str, Any]]]:
        return {table: await table_rows(session, table) for table in tables}

    export.__name__ = f"export_{'_'.join(tables)}"
    return export


async def build_seed_json(session: AsyncSession) -> dict[str, Any]:
    """``{ meta, tables, enums, config }`` — SPEC_RESOLUTIONS #16."""
    from app.core.api.meta import build_config, build_enums

    ctx = framework.SeedContext.create(0)
    tables: dict[str, list[dict[str, Any]]] = {}
    for name, exporter in framework.exporters():
        for table, rows in (await exporter(session, ctx)).items():
            if table in tables:
                raise ValueError(f"seed exporter {name}: table {table!r} exported twice")
            tables[table] = rows
    return {
        "meta": {
            "version": SEED_JSON_VERSION,
            "generatedAt": _json_value(now_utc()),
            "anchorDate": ctx.today.isoformat(),
        },
        "tables": tables,
        "enums": build_enums().model_dump(mode="json", by_alias=True),
        "config": build_config().model_dump(mode="json", by_alias=True),
    }


async def export_seed_json(session: AsyncSession, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - CLI only
    target = directory / "seed.json"
    target.write_bytes(
        orjson.dumps(await build_seed_json(session), option=orjson.OPT_INDENT_2) + b"\n"
    )
    return target
