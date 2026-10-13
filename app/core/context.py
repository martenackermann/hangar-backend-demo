"""Request/job context variables (SPEC_RESOLUTIONS #14).

``notify()`` and log lines default their ``actor``/``trigger`` from here: the auth
dependency sets the actor for requests; job runners set ``Trigger.JOB`` (or ``IMPORT``,
``SYNC``) with :func:`job_context`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.core.enums import Trigger

current_actor_id: ContextVar[int | None] = ContextVar("current_actor_id", default=None)
current_trigger: ContextVar[Trigger] = ContextVar("current_trigger", default=Trigger.USER)


@contextmanager
def job_context(trigger: Trigger = Trigger.JOB, actor_id: int | None = None) -> Iterator[None]:
    token_t = current_trigger.set(trigger)
    token_a = current_actor_id.set(actor_id)
    try:
        yield
    finally:
        current_trigger.reset(token_t)
        current_actor_id.reset(token_a)
