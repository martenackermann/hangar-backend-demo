"""App wiring: module discovery, OpenAPI contract, CLI export."""

import json
from pathlib import Path

from click.testing import CliRunner
from fastapi import FastAPI

from app.cli import cli
from app.core.discovery import discover, module_names
from app.core.pagination import Page
from app.core.schemas import ApiModel

EXPECTED_MODULES = {
    "core",
    "requests_comms",
    "imports_master",
    "staging_fulfilment",
    "field_service",
    "stock_inventory",
}


def test_module_packages_are_discovered() -> None:
    assert set(module_names()) >= EXPECTED_MODULES
    for name, module in discover("router"):
        assert module.__name__ == f"app.modules.{name}.router"


def test_system_routes_are_mounted(app: FastAPI) -> None:
    paths = set(app.openapi()["paths"])
    assert {"/api/v1/health", "/api/v1/jobs/{job_id}", "/api/v1/files/{token}"} <= paths


def test_openapi_uses_problem_details(app: FastAPI) -> None:
    schema = app.openapi()
    components = schema["components"]["schemas"]
    assert {"ProblemDetails", "ValidationProblem"} <= set(components)
    assert "HTTPValidationError" not in components
    job = schema["paths"]["/api/v1/jobs/{job_id}"]["get"]
    assert job["operationId"] == "jobs-get_job"
    assert set(job["responses"]["422"]["content"]) == {"application/problem+json"}
    assert job["responses"]["404"]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/ProblemDetails"
    }
    health = components["HealthOut"]["properties"]
    assert "demoMode" in health  # camelCase in the contract


def test_page_schema_name() -> None:
    class ThingOut(ApiModel):
        id: int

    assert Page[ThingOut].model_json_schema()["title"] == "ThingOutPage"


def test_openapi_cli_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"
    result = CliRunner().invoke(cli, ["openapi", "--output", str(target)])
    assert result.exit_code == 0, result.output
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["info"]["title"] == "Hangar API"
    assert "/api/v1/health" in document["paths"]
