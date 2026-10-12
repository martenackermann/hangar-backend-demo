"""RFC 9457 problem details (``application/problem+json``) for every error response.

Shape::

    {"type": "...", "title": "...", "status": 409, "detail": "...", "instance": "/api/v1/...",
     "requestId": "...", ...extension members}

Validation errors (422) add ``errors: {"field.path": ["message", ...]}`` where the field
path uses the camelCase JSON names. Raise the exception classes below from services;
never build error responses by hand.
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


def problem_type(slug: str) -> str:
    return f"{PROBLEM_BASE}{slug}"


# --- schemas (documented in OpenAPI) ---------------------------------------------------


class ProblemDetails(BaseModel):
    """RFC 9457 problem details. Extension members may be present."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(default="about:blank", description="URI identifying the problem type.")
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    request_id: str | None = Field(default=None, alias="requestId")


class ValidationProblem(ProblemDetails):
    errors: dict[str, list[str]] = Field(
        description="Field path (camelCase, dot-separated) -> messages."
    )


# --- exceptions ------------------------------------------------------------------------


class ApiError(Exception):
    """Base class for errors rendered as problem details.

    Subclasses set ``status``/``slug``/``title``; any extra keyword arguments become
    extension members of the problem body (keep them camelCase).
    """

    status: int = 500
    slug: str | None = None
    title: str | None = None

    def __init__(
        self,
        detail: str | None = None,
        *,
        status: int | None = None,
        title: str | None = None,
        slug: str | None = None,
        headers: Mapping[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(detail or title or self.title or "")
        self.detail = detail
        if status is not None:
            self.status = status
        if title is not None:
            self.title = title
        if slug is not None:
            self.slug = slug
        self.headers = dict(headers or {})
        self.extensions = extensions

    @property
    def type(self) -> str:
        return problem_type(self.slug) if self.slug else "about:blank"

    @property
    def resolved_title(self) -> str:
        return self.title or HTTPStatus(self.status).phrase


class BadRequestError(ApiError):
    status = 400
    slug = "bad-request"
    title = "Bad request"


class AuthenticationError(ApiError):
    status = 401
    slug = "unauthenticated"
    title = "Authentication required"

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(detail, **kwargs)


class PermissionDeniedError(ApiError):
    status = 403
    slug = "forbidden"
    title = "Permission denied"


class NotFoundError(ApiError):
    status = 404
    slug = "not-found"
    title = "Resource not found"


class ConflictError(ApiError):
    status = 409
    slug = "conflict"
    title = "Conflict"


class GoneError(ApiError):
    status = 410
    slug = "gone"
    title = "Gone"


class DomainValidationError(ApiError):
    """422 raised by services, same shape as request validation errors."""

    status = 422
    slug = "validation-error"
    title = "Validation failed"

    def __init__(
        self,
        errors: Mapping[str, Sequence[str] | str],
        detail: str | None = None,
        **kwargs: Any,
    ) -> None:
        normalized = {
            field: [messages] if isinstance(messages, str) else list(messages)
            for field, messages in errors.items()
        }
        super().__init__(detail or "One or more fields are invalid.", errors=normalized, **kwargs)


class TransitionError(ApiError):
    """A workflow action is not allowed for the entity's current state (409)."""

    status = 409
    slug = "transition-not-allowed"
    title = "Transition not allowed"

    def __init__(
        self,
        detail: str,
        *,
        action: str,
        current_state: str,
        allowed_actions: Sequence[str] = (),
        entity: str | None = None,
        entity_id: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            detail,
            action=action,
            currentState=current_state,
            allowedActions=list(allowed_actions),
            entity=entity,
            entityId=entity_id,
            **kwargs,
        )
        self.action = action
        self.current_state = current_state
        self.allowed_actions = list(allowed_actions)


# --- rendering -------------------------------------------------------------------------


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def problem_response(
    request: Request,
    *,
    status: int,
    title: str | None = None,
    type_: str = "about:blank",
    detail: str | None = None,
    headers: Mapping[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_,
        "title": title or HTTPStatus(status).phrase,
        "status": status,
    }
    if detail:
        body["detail"] = detail
    body["instance"] = request.url.path
    if (request_id := _request_id(request)) is not None:
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
        field = _field_path(error.get("loc", ()))
        result.setdefault(field, []).append(_clean_message(str(error.get("msg", "Invalid value"))))
    return result


async def _handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    if exc.status >= 500:
        log.error("api_error", status=exc.status, detail=exc.detail)
    return problem_response(
        request,
        status=exc.status,
        title=exc.resolved_title,
        type_=exc.type,
        detail=exc.detail,
        headers=exc.headers,
        **exc.extensions,
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    phrase = HTTPStatus(exc.status_code).phrase
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != phrase else None
    return problem_response(
        request,
        status=exc.status_code,
        title=phrase,
        detail=detail,
        headers=exc.headers,
    )


async def _handle_request_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return problem_response(
        request,
        status=422,
        title="Validation failed",
        type_=problem_type("validation-error"),
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
        title="Conflict",
        type_=problem_type("conflict"),
        detail="The request conflicts with existing data.",
        constraint=constraint,
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_exception", exc_type=type(exc).__name__)
    return problem_response(
        request,
        status=500,
        title="Internal Server Error",
        detail="An unexpected error occurred.",
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_request_validation)
    app.add_exception_handler(IntegrityError, _handle_integrity_error)
    app.add_exception_handler(Exception, _handle_unexpected)


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses=`` helper: ``responses=problem_responses(401, 404, 409)``.

    The referenced component schemas are injected by ``app.core.openapi``.
    """
    return {
        status: {
            "description": HTTPStatus(status).phrase,
            "content": {
                PROBLEM_JSON: {
                    "schema": {
                        "$ref": "#/components/schemas/"
                        + ("ValidationProblem" if status == 422 else "ProblemDetails")
                    }
                }
            },
        }
        for status in statuses
    }
