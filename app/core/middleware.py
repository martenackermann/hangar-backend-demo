"""ASGI middleware: request id + one structured access-log line per request, and the
request body size limit."""

from __future__ import annotations

import re
import time
from uuid import uuid4

import orjson
import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import PROBLEM_JSON, problem_type

log = structlog.get_logger("hangar.access")

REQUEST_ID_HEADER = "X-Request-Id"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: Sent with every response (JSON API + downloads; nothing here is meant to be sniffed/framed).
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


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
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
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


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """``413 too_large`` for request bodies over the limit, *while* they stream in.

    Multipart uploads may be ``upload_bytes`` (+ 1 MiB of form overhead); every other body
    ``max_bytes``. A declared ``Content-Length`` is rejected up front; chunked bodies are
    counted and cut off as soon as they cross the limit, before the multipart parser spools
    them to disk.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int, upload_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.upload_bytes = upload_bytes + 1024 * 1024

    def _limit(self, scope: Scope) -> int:
        for key, value in scope["headers"]:
            if key == b"content-type" and value.lower().startswith(b"multipart/form-data"):
                return self.upload_bytes
        return self.max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope)
        declared = next((v for k, v in scope["headers"] if k == b"content-length"), None)
        if declared is not None:
            try:
                too_large = int(declared) > limit
            except ValueError:
                too_large = False  # the server rejects malformed lengths itself
            if too_large:
                await self._reject(scope, send, limit)
                return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if exceeded:
                return  # drop the app's own error response; the 413 below replaces it
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            pass
        except Exception:
            if not exceeded:
                raise
        if exceeded and not response_started:
            await self._reject(scope, send, limit)

    @staticmethod
    async def _reject(scope: Scope, send: Send, limit: int) -> None:
        body: dict[str, object] = {
            "type": problem_type("too_large"),
            "title": "Content Too Large",
            "status": 413,
            "detail": f"The request body is larger than {limit // (1024 * 1024)} MB.",
            "instance": scope.get("path", ""),
            "code": "too_large",
        }
        request_id = scope.get("state", {}).get("request_id")
        if request_id is not None:
            body["requestId"] = request_id
        payload = orjson.dumps(body)
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", PROBLEM_JSON.encode()),
                    (b"content-length", str(len(payload)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
