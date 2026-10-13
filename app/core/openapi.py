"""OpenAPI post-processing so the published contract matches runtime behavior.

- every 422 response references ``Problem`` as ``application/problem+json``;
- the ``Problem`` schema is always present (FastAPI's ``HTTPValidationError`` is removed);
- operationIds are ``<tag>-<function_name>`` (see ``unique_operation_id``).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from app.core.errors import PROBLEM_JSON, Problem

_REF_TEMPLATE = "#/components/schemas/{model}"


def unique_operation_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}" if route.tags else route.name


def _problem_schemas() -> dict[str, Any]:
    schema = Problem.model_json_schema(ref_template=_REF_TEMPLATE, by_alias=True)
    defs = schema.pop("$defs", {})
    return {**defs, "Problem": schema}


def build_openapi(app: FastAPI) -> dict[str, Any]:
    schema = get_openapi(
        title=app.title,
        version=app.version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
        servers=app.servers,
    )
    components = schema.setdefault("components", {})
    schemas: dict[str, Any] = components.setdefault("schemas", {})
    schemas.update(_problem_schemas())
    problem_ref = {"$ref": _REF_TEMPLATE.format(model="Problem")}
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            responses = operation.get("responses", {}) if isinstance(operation, dict) else {}
            if "422" in responses:
                responses["422"] = {
                    "description": "Validation failed",
                    "content": {PROBLEM_JSON: {"schema": problem_ref}},
                }
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    return schema


def install_openapi(app: FastAPI) -> None:
    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app)
        return app.openapi_schema

    app.openapi = openapi
