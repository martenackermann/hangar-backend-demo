"""RFC 9457 problem details (API.md §1.4).

Every error response is ``application/problem+json``::

    {"type": "https://hangar.example.com/problems/<code>", "title": "...", "status": 409,
     "detail": "...", "instance": "/api/v1/...", "code": "<code>", ...extensions}

Extensions by code: ``errors`` (422), ``guard`` (409 guard_failed), ``allowedActions``
(409 transition_not_allowed), ``currentVersion`` (409 version_conflict). Raise the
exception classes below; never build error bodies by hand.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"
PROBLEM_BASE = "https://hangar.example.com/problems/"

log = structlog.get_logger(__name__)

_DEFAULT_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    500: "internal_error",
    502: "bad_gateway",
    503: "unavailable",
}


def problem_type(code: str) -> str:
    return f"{PROBLEM_BASE}{code}"


# --- OpenAPI schemas -----------------------------------------------------------------------


class GuardInfo(BaseModel):
    code: str
    message: str


class Problem(BaseModel):
    """RFC 9457 problem details (API.md ``Problem``)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str
    title: str
    status: int
    detail: str | None
    instance: str
    code: str
    errors: dict[str, list[str]] | None = Field(default=None, description="422 only")
    guard: GuardInfo | None = Field(default=None, description="409 guard_failed only")
    allowed_actions: list[str] | None = Field(default=None, alias="allowedActions")
    current_version: int | None = Field(default=None, alias="currentVersion")


# --- exceptions ----------------------------------------------------------------------------


class ApiError(Exception):
    """Base class for problem responses. Extra kwargs become extension members (camelCase)."""

    status: int = 500
    code: str = "internal_error"
    title: str | None = None

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        status: int | None = None,
        title: str | None = None,
        headers: Mapping[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(detail or title or self.title or self.code)
        self.detail = detail
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        if title is not None:
            self.title = title
        self.headers = dict(headers or {})
        self.extensions = extensions

    @property
    def resolved_title(self) -> str:
        return self.title or HTTPStatus(self.status).phrase


class BadRequestError(ApiError):
    status, code, title = 400, "bad_request", "Bad request"


class AuthenticationError(ApiError):
    status, code, title = 401, "unauthenticated", "Authentication required"

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(detail, **kwargs)


class ForbiddenError(ApiError):
    status, code, title = 403, "forbidden", "Permission denied"


class NotFoundError(ApiError):
    status, code, title = 404, "not_found", "Not found"


class ConflictError(ApiError):
    status, code, title = 409, "conflict", "Conflict"


class ValidationProblem(ApiError):
    """422 ``validation_error`` raised by services (same shape as request validation)."""

    status, code, title = 422, "validation_error", "Validation failed"

    def __init__(
        self,
        errors: Mapping[str, Sequence[str] | str],
        detail: str | None = None,
        **kwargs: Any,
    ) -> None:
        normalized = {
            key: [value] if isinstance(value, str) else list(value) for key, value in errors.items()
        }
        super().__init__(detail or "One or more fields are invalid.", errors=normalized, **kwargs)


class TransitionNotAllowed(ApiError):
    """409 ``transition_not_allowed`` (carries ``allowedActions``)."""

    status, code, title = 409, "transition_not_allowed", "Action not allowed"

    def __init__(
        self,
        detail: str,
        *,
        action: str,
        current_status: str | None,
        allowed_actions: Sequence[str] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(detail, allowedActions=list(allowed_actions), **kwargs)
        self.action = action
        self.current_status = current_status
        self.allowed_actions = list(allowed_actions)


class GuardFailed(ApiError):
    """409 ``guard_failed`` (carries ``guard: {code, message}``)."""

    status, code, title = 409, "guard_failed", "Precondition failed"

    def __init__(self, guard_code: str, message: str, **kwargs: Any) -> None:
        super().__init__(message, guard={"code": guard_code, "message": message}, **kwargs)
        self.guard_code = guard_code
        self.guard_message = message


class VersionConflict(ApiError):
    status, code, title = 409, "version_conflict", "Version conflict"

    def __init__(self, current_version: int, **kwargs: Any) -> None:
        super().__init__(
            "The resource was changed by someone else. Reload and try again.",
            currentVersion=current_version,
            **kwargs,
        )
        self.current_version = current_version


class GatewayError(ApiError):
    """502 for gateway failures (``carrier_error`` / ``booking_error``); nothing changed."""

    status, code, title = 502, "bad_gateway", "External system error"


class UnavailableError(ApiError):
    status, code, title = 503, "unavailable", "Service unavailable"


# --- rendering -----------------------------------------------------------------------------


def problem_response(
    request: Request,
    *,
    status: int,
    code: str | None = None,
    title: str | None = None,
    detail: str | None = None,
    headers: Mapping[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    resolved_code = code or _DEFAULT_CODES.get(status, "error")
    body: dict[str, Any] = {
        "type": problem_type(resolved_code),
        "title": title or HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "code": resolved_code,
    }
    request_id = getattr(request.state, "request_id", None)
    if request_id is not None:
        body["requestId"] = request_id
    body.update({k: v for k, v in extensions.items() if v is not None})
    return JSONResponse(
        body, status_code=status, headers=dict(headers or {}), media_type=PROBLEM_JSON
    )


_LOCATION_ROOTS = {"body", "query", "path", "header", "cookie"}


def _field_path(loc: Sequence[int | str]) -> str:
    parts = list(loc)
    if len(parts) > 1 and parts[0] in _LOCATION_ROOTS:
        parts = parts[1:]
    return ".".join(str(p) for p in parts) or "body"


def _clean_message(message: str) -> str:
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            return message[len(prefix) :]
    return message


def validation_errors_to_dict(errors: Sequence[Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for error in errors:
        loc = error.get("loc", ())
        if error.get("type") == "extra_forbidden":
            message = "Unknown field."
        else:
            message = _clean_message(str(error.get("msg", "Invalid value")))
        result.setdefault(_field_path(loc), []).append(message)
    return result


async def _handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    if exc.status >= 500:
        log.error("api_error", status=exc.status, code=exc.code, detail=exc.detail)
    return problem_response(
        request,
        status=exc.status,
        code=exc.code,
        title=exc.resolved_title,
        detail=exc.detail,
        headers=exc.headers,
        **exc.extensions,
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    phrase = HTTPStatus(exc.status_code).phrase
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != phrase else None
    return problem_response(
        request, status=exc.status_code, title=phrase, detail=detail, headers=exc.headers
    )


async def _handle_request_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return problem_response(
        request,
        status=422,
        code="validation_error",
        title="Validation failed",
        detail="One or more fields are invalid.",
        errors=validation_errors_to_dict(exc.errors()),
    )


async def _handle_integrity_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, IntegrityError)
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint = getattr(diag, "constraint_name", None)
    log.warning("integrity_error", constraint=constraint)
    return problem_response(
        request,
        status=409,
        code="conflict",
        title="Conflict",
        detail="The request conflicts with existing data.",
        constraint=constraint,
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_exception", exc_type=type(exc).__name__)
    return problem_response(
        request, status=500, code="internal_error", detail="An unexpected error occurred."
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_request_validation)
    app.add_exception_handler(IntegrityError, _handle_integrity_error)
    app.add_exception_handler(Exception, _handle_unexpected)


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses=`` helper: ``responses=problem_responses(401, 403, 404)``."""
    return {
        status: {
            "description": HTTPStatus(status).phrase,
            "content": {PROBLEM_JSON: {"schema": {"$ref": "#/components/schemas/Problem"}}},
        }
        for status in statuses
    }
