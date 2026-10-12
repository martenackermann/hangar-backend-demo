"""structlog configuration: JSON lines (default) or a dev console renderer.

stdlib loggers (uvicorn, sqlalchemy, procrastinate, alembic) are routed through the same
formatter, so every line on stdout has the same shape. Use
``log = structlog.get_logger(__name__)`` and pass context as keyword arguments.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import orjson
import structlog
from structlog.types import EventDict, Processor


def _orjson_dumps(value: Any, **_: Any) -> str:
    return orjson.dumps(value, default=str).decode()


def _drop_color_message(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """uvicorn adds an ANSI-colored duplicate of the message as an ``extra``."""
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        _drop_color_message,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: Processor
    if fmt == "console":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        shared.append(structlog.processors.dict_tracebacks)
        renderer = structlog.processors.JSONRenderer(serializer=_orjson_dumps)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "procrastinate", "alembic"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # Access lines come from RequestContextMiddleware instead; registration chatter is noise.
    for name in (
        "uvicorn.access",
        "alembic.runtime.plugins",
        "procrastinate.blueprints",
        "procrastinate.periodic",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)
