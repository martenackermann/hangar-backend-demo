"""ASGI middleware: request id + one structured access-log line per request."""

from __future__ import annotations

import re
import time
from uuid import uuid4

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = structlog.get_logger("hangar.access")

REQUEST_ID_HEADER = "X-Request-Id"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = next(
            (v.decode("latin-1") for k, v in scope["headers"] if k == b"x-request-id"), ""
        )
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                # Successful health probes (Docker/k8s, every few seconds) are debug-level.
                quiet = scope["path"].endswith(("/healthz", "/readyz")) and status_code < 400
                (log.debug if quiet else log.info)(
                    "request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
