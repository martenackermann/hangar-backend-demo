"""Management CLI: ``uv run hangar <command>`` (SPEC §2.2 + SPEC_RESOLUTIONS #52).

hangar serve [--reload]
hangar db upgrade | db reset [--yes] [--seed]
hangar seed [--reset] [--export-json DIR]
hangar demo-reset
hangar worker [-q QUEUE] [--once]
hangar openapi [-o FILE]
hangar jobs-schema
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from app.core.aio import UVICORN_LOOP, run
from app.core.config import get_settings
from app.core.logging import configure_logging


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Hangar API management commands."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)


@cli.command()
@click.option("--host", default=None, help="Bind host (default: HOST or 127.0.0.1).")
@click.option("--port", type=int, default=None, help="Bind port (default: PORT or 8100).")
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (dev).")
def serve(host: str | None, port: int | None, reload: bool) -> None:
    """Run the API with uvicorn."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        reload_dirs=["app"] if reload else None,
        loop=UVICORN_LOOP,
        log_config=None,
        access_log=False,
        proxy_headers=True,
    )


@cli.group()
def db() -> None:
    """Schema management."""


@db.command("upgrade")
def db_upgrade() -> None:
    """``alembic upgrade heads`` + procrastinate schema (idempotent)."""
    from app.core.dbadmin import migrate_jobs_schema, upgrade_heads

    upgrade_heads()
    run(migrate_jobs_schema())
    click.echo("Database upgraded.")


@db.command("reset")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.option("--seed", "with_seed", is_flag=True, help="Seed afterwards.")
def db_reset(yes: bool, with_seed: bool) -> None:
    """DROP the schema and recreate it with create_all (dev/test only, no Alembic)."""
    from app.core.dbadmin import reset_database

    settings = get_settings()
    if settings.is_production:
        raise click.ClickException("Refusing to reset a production database.")
    if not yes:
        click.confirm(
            f"Drop ALL data in {settings.psycopg_conninfo.rsplit('/', 1)[-1]}?", abort=True
        )
    run(reset_database())
    click.echo("Database reset.")
    if with_seed:
        _seed(reset=False, export=None)


def _seed(*, reset: bool, export: Path | None) -> None:
    from app.core.db import commit, open_session
    from app.core.demo import export_seed_json, reset_and_seed
    from app.core.seed.framework import run_seeders
    from app.core.slices import load_slices

    load_slices()

    async def go() -> list[str] | None:
        async with open_session() as session:
            ran: list[str] | None = None
            if reset:
                await reset_and_seed(session)
            else:
                ran = await run_seeders(session)
            await commit(session)
            if export is not None:
                target = await export_seed_json(session, export)
                click.echo(f"Wrote {target}")
            return ran

    ran = run(go())
    click.echo("Seeded (reset)." if reset else f"Seeded: {', '.join(ran or []) or '(no seeders)'}")


@cli.command()
@click.option("--reset", is_flag=True, help="Truncate demo data first (like demo.reset).")
@click.option(
    "--export-json",
    "export",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Also write seed.json for the frontend MSW demo into this directory.",
)
def seed(reset: bool, export: Path | None) -> None:
    """Run the seed pipeline (idempotent) and optionally export ``seed.json``."""
    _refuse_demo_data_in_production()
    _seed(reset=reset, export=export)


@cli.command("demo-reset")
def demo_reset() -> None:
    """Truncate demo data and re-seed (synchronous ``demo.reset``)."""
    _refuse_demo_data_in_production()
    _seed(reset=True, export=None)


def _refuse_demo_data_in_production() -> None:
    from app.core.demo import demo_reset_allowed

    if not demo_reset_allowed():
        raise click.ClickException(
            "Refusing to load demo data in production outside demo mode: the demo personas "
            "share a published password. Set DEMO_MODE=true for a demo deployment."
        )


@cli.command("jobs-schema")
def jobs_schema() -> None:
    """Apply the procrastinate schema if missing."""
    from app.core.dbadmin import migrate_jobs_schema

    applied = run(migrate_jobs_schema())
    click.echo("Job schema applied." if applied else "Job schema already present.")


@cli.command()
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Target file (default: openapi.json at the repo root).",
)
def openapi(output: Path | None) -> None:
    """Export the OpenAPI document."""
    from app.core.dbadmin import REPO_ROOT
    from app.main import create_app

    target = output or REPO_ROOT / "openapi.json"
    schema = create_app().openapi()
    text = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    target.write_text(text, encoding="utf-8", newline="\n")  # LF on every OS
    click.echo(f"Wrote {target} ({len(schema.get('paths', {}))} paths).")


@cli.command()
@click.option(
    "-q", "--queue", "queues", multiple=True, help="Queue(s) to listen to (default: all)."
)
@click.option("-c", "--concurrency", type=int, default=None)
@click.option("--once", is_flag=True, help="Process available jobs, then exit.")
def worker(queues: tuple[str, ...], concurrency: int | None, once: bool) -> None:
    """Run the background job worker."""
    from app.jobs.worker import run_worker

    try:
        run(run_worker(queues=queues or None, concurrency=concurrency, wait=not once))
    except KeyboardInterrupt:
        click.echo("Worker stopped.")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
