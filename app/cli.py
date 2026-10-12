"""Management CLI: ``uv run hangar <command>``."""

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


@cli.command("db-reset")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.option("--seed", "with_seed", is_flag=True, help="Run seeders afterwards.")
def db_reset(yes: bool, with_seed: bool) -> None:
    """DROP and recreate the schema, create all tables and the job schema (dev only)."""
    from app.core.dbadmin import reset_database, run_seeders

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
        click.echo(f"Seeded: {', '.join(run(run_seeders())) or '(no seeders)'}")


@cli.command()
def migrate() -> None:
    """Apply Alembic migrations (create_all while none exist) and the job schema."""
    from app.core.dbadmin import migrate_jobs_schema, run_migrations

    run_migrations()
    run(migrate_jobs_schema())
    click.echo("Migrations applied.")


@cli.command()
def seed() -> None:
    """Run all module seeders (``app/modules/*/seed.py``)."""
    from app.core.dbadmin import run_seeders

    ran = run(run_seeders())
    click.echo(f"Seeded: {', '.join(ran) or '(no seeders)'}")


@cli.command("jobs-schema")
def jobs_schema() -> None:
    """Apply the procrastinate schema if missing (``procrastinate schema --apply``)."""
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
