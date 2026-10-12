"""Event-loop helpers.

psycopg's async mode cannot run on Windows' default ``ProactorEventLoop``; every entry
point that starts an event loop (CLI, worker, Alembic, uvicorn, pytest) goes through
these helpers so the selector loop is used on Windows and the default elsewhere.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any

IS_WINDOWS = sys.platform == "win32"

#: Value for ``uvicorn.run(loop=...)`` / ``uvicorn --loop``.
UVICORN_LOOP = "asyncio:SelectorEventLoop" if IS_WINDOWS else "auto"


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    return asyncio.SelectorEventLoop if IS_WINDOWS else None


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run`` with a psycopg-compatible loop."""
    return asyncio.run(coro, loop_factory=loop_factory())
