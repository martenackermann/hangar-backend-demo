"""Worker entry point: ``uv run hangar-worker`` / ``uv run hangar worker`` /
``python -m app.jobs.worker``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import structlog
from procrastinate.app import WorkerOptions

from app.core.aio import run
from app.core.config import get_settings
from app.core.discovery import import_subscribers
from app.core.logging import configure_logging

log = structlog.get_logger("hangar.worker")


async def run_worker(
    *, queues: Sequence[str] | None = None, concurrency: int | None = None, wait: bool = True
) -> None:
    from app.jobs.app import job_app

    import_subscribers()
    settings = get_settings()
    log.info("worker_starting", queues=list(queues or []) or "all", tasks=sorted(job_app.tasks))
    options: WorkerOptions = {
        "concurrency": concurrency or settings.worker_concurrency,
        "wait": wait,
        "name": "hangar-worker",
    }
    if queues:
        options["queues"] = list(queues)
    async with job_app.open_async():
        await job_app.run_worker_async(**options)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hangar-worker", description="Run the job worker.")
    parser.add_argument(
        "-q",
        "--queue",
        action="append",
        dest="queues",
        help="Queue(s) to listen to (default: all).",
    )
    parser.add_argument("-c", "--concurrency", type=int, default=None)
    parser.add_argument("--once", action="store_true", help="Process available jobs, then exit.")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        run(run_worker(queues=args.queues, concurrency=args.concurrency, wait=not args.once))
    except KeyboardInterrupt:
        log.info("worker_stopped")


if __name__ == "__main__":
    main()
