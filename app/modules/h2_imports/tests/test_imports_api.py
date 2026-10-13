"""``/imports`` endpoints: upload, detect, history, detail, issues, cancel, downloads, roles."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ImportJobStatus
from app.core.models import BackgroundJob
from app.core.notifications import render, templates
from app.modules.h2_imports.models import ImportJob

from .conftest import SR_HEADER, csv_bytes, sr_row

DANA = "dana.dispatcher@example.com"
THEO = "theo.technician@example.com"
VIC = "vic.viewer@example.com"
ADA = "ada.admin@example.com"
Headers = Callable[[str], Awaitable[dict[str, str]]]


def srs(*rows: dict[str, Any]) -> bytes:
    return csv_bytes(SR_HEADER, list(rows))


async def test_upload_queues_job_and_task(
    db_session: AsyncSession, upload: Any, jobs: InMemoryConnector
) -> None:
    response = await upload(srs(sr_row("SR0098001")), filename="itsm.csv")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    job = await db_session.get(ImportJob, body["importJobId"])
    assert job is not None
    assert (job.status, job.type, job.type_detected, job.total_rows) == (
        ImportJobStatus.QUEUED,
        "service_requests",
        True,
        1,
    )
    assert job.background_job_id == body["jobId"]
    background = await db_session.get(BackgroundJob, body["jobId"])
    assert background is not None
    assert (background.entity_type, background.entity_id) == ("import_job", job.id)
    [task] = [j for j in jobs.jobs.values() if j["task_name"] == "imports.process"]
    assert task["queue_name"] == "imports"
    assert task["args"] == {"job_id": body["jobId"], "import_job_id": job.id}


async def test_forced_type_and_semicolon_csv(upload: Any) -> None:
    content = csv_bytes(
        ["user_id", "last_name"], [{"user_id": "T850001", "last_name": "X"}], delimiter=";"
    )
    response = await upload(content, import_type="contacts")
    assert response.status_code == 202


@pytest.mark.parametrize(
    ("content", "filename", "status", "code"),
    [
        (b"user_id,email\nE1,a@example.com\n", "x.csv", 422, "type_not_detected"),
        (
            b"sr_number,order_details\nSR0012345,[Procurement type: Removal]\n",
            "x.csv",
            422,
            "missing_columns",
        ),
        ("user_id,last_name\nE1,\xe4\n".encode("latin-1"), "x.csv", 400, "encoding_unsupported"),
        (b"user_id,user id,last_name\nA,B,C\n", "x.csv", 400, "duplicate_columns"),
        (b"user_id,last_name\n", "x.csv", 400, "empty_file"),
        (b"user_id,last_name\nE1,X\n", "x.txt", 415, "unsupported_media_type"),
        (b"not a zip", "x.xlsx", 400, "invalid_file"),
    ],
)
async def test_upload_file_problems(
    upload: Any, content: bytes, filename: str, status: int, code: str
) -> None:
    response = await upload(content, filename=filename)
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


async def test_missing_columns_lists_them(upload: Any) -> None:
    response = await upload(b"sr_number,order_details\nSR0012345,x\n")
    errors = response.json()["errors"]["file"][0]
    assert "user_id" in errors
    assert "delivery_address" in errors
    assert "last_name" in errors


async def test_options_validation(upload: Any) -> None:
    response = await upload(srs(sr_row("SR0098001")), options={"dryRun": "maybe"})
    assert response.status_code == 422
    assert "options.dryRun" in response.json()["errors"]
    response = await upload(srs(sr_row("SR0098001")), options={"unknown": True})
    assert "options.unknown" in response.json()["errors"]


async def test_detect_preview(client: AsyncClient, headers: Headers) -> None:
    content = srs(*(sr_row(f"SR009800{i}") for i in range(7)))
    response = await client.post(
        "/api/v1/imports/detect",
        files={"file": ("x.csv", content, "text/csv")},
        headers=await headers(DANA),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["detectedType"] == "service_requests"
    assert body["delimiter"] == ","
    assert body["rowCount"] == 7
    assert len(body["preview"]) == 5
    assert body["preview"][0]["sr_number"] == "SR0098000"
    assert body["missingColumns"] == []
    assert body["problems"] == []

    bad = await client.post(
        "/api/v1/imports/detect",
        files={"file": ("x.csv", b"user_id,email\nE1,x\n", "text/csv")},
        headers=await headers(DANA),
    )
    assert bad.json()["detectedType"] is None
    assert bad.json()["problems"][0]["code"] == "type_not_detected"
    broken = await client.post(
        "/api/v1/imports/detect",
        files={"file": ("x.csv", "a,b\n\xe4,1\n".encode("latin-1"), "text/csv")},
        headers=await headers(DANA),
    )
    assert broken.json()["problems"][0]["code"] == "encoding_unsupported"


async def test_history_detail_issues_and_downloads(
    client: AsyncClient, headers: Headers, run_file: Any
) -> None:
    result = await run_file(srs(sr_row("SR0098101"), sr_row("SR12AB", task_number="")))
    auth = await headers(VIC)
    listing = await client.get(
        "/api/v1/imports?type=service_requests&status=completed_with_errors", headers=auth
    )
    assert listing.status_code == 200
    first = listing.json()["data"][0]
    assert first["id"] == result.import_job_id
    assert first["counts"]["errors"] == 1
    assert first["progressPercent"] == 100
    assert first["availableActions"] == []
    assert first["createdBy"]["email"] == DANA

    detail = (await client.get(f"/api/v1/imports/{result.import_job_id}", headers=auth)).json()
    assert detail["hasErrorReport"] is True
    assert detail["topErrorCodes"] == [{"code": "invalid_format", "severity": "error", "count": 1}]
    assert detail["postProcess"]["contactsCreated"] == 1
    assert detail["options"]["dryRun"] is False

    issues = await client.get(
        f"/api/v1/imports/{result.import_job_id}/errors?severity=error&code=invalid_format",
        headers=auth,
    )
    [issue] = issues.json()["data"]
    assert issue["rowNumber"] == 3
    assert issue["columnName"] == "sr_number"
    assert issue["rawValue"] == "SR12AB"
    assert issue["businessKey"] == "SR12AB"
    none = await client.get(
        f"/api/v1/imports/{result.import_job_id}/errors?severity=warning", headers=auth
    )
    assert none.json()["meta"]["total"] == 0

    report = await client.get(f"/api/v1/imports/{result.import_job_id}/error-report", headers=auth)
    assert report.status_code == 200
    downloaded = await client.get("/api/v1" + report.json()["url"].split("/api/v1", 1)[1])
    assert downloaded.status_code == 200
    assert "invalid_format" in downloaded.text
    original = await client.get(f"/api/v1/imports/{result.import_job_id}/file", headers=auth)
    assert original.json()["filename"] == "export.csv"


async def test_error_report_404_without_issues(
    client: AsyncClient, headers: Headers, run_file: Any
) -> None:
    result = await run_file(srs(sr_row("SR0098201")))
    response = await client.get(
        f"/api/v1/imports/{result.import_job_id}/error-report", headers=await headers(DANA)
    )
    assert response.status_code == 404


async def test_cancel_queued_and_terminal(
    client: AsyncClient, headers: Headers, upload: Any, process: Any
) -> None:
    import_job_id = (await upload(srs(sr_row("SR0098301")))).json()["importJobId"]
    auth = await headers(DANA)
    queued = (await client.get(f"/api/v1/imports/{import_job_id}", headers=auth)).json()
    assert queued["availableActions"] == ["cancel"]
    response = await client.post(
        f"/api/v1/imports/{import_job_id}/actions/cancel", json={}, headers=auth
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    result = await process(import_job_id)  # the worker sees the cancellation and does nothing
    assert result.status == ImportJobStatus.CANCELLED
    again = await client.post(
        f"/api/v1/imports/{import_job_id}/actions/cancel", json={}, headers=auth
    )
    assert again.status_code == 409
    assert again.json()["code"] == "transition_not_allowed"


async def test_cancel_processing_is_cooperative(
    db_session: AsyncSession, client: AsyncClient, headers: Headers, upload: Any
) -> None:
    import_job_id = (await upload(srs(sr_row("SR0098401")))).json()["importJobId"]
    job = await db_session.get(ImportJob, import_job_id)
    assert job is not None
    job.status = ImportJobStatus.PROCESSING
    await db_session.flush()
    response = await client.post(
        f"/api/v1/imports/{import_job_id}/actions/cancel", json={}, headers=await headers(DANA)
    )
    body = response.json()
    assert body["status"] == "processing"
    assert body["availableActions"] == []
    await db_session.refresh(job)
    assert job.cancel_requested is True


async def test_templates_and_samples(client: AsyncClient, headers: Headers) -> None:
    auth = await headers(DANA)
    templates_body = (await client.get("/api/v1/imports/templates", headers=auth)).json()["data"]
    by_type = {t["type"]: t for t in templates_body}
    assert set(by_type) == {
        "lease_returns",
        "service_requests",
        "contacts",
        "assets_new",
        "asset_status_sync",
    }
    sr = by_type["service_requests"]
    assert sr["signatureHeaders"] == ["order_details"]
    assert sr["upsertKey"] == "sr_number"
    assert {p["column"] for p in sr["packedFields"]} == {"order_details", "delivery_address"}
    assert by_type["assets_new"]["options"][0] == {
        "name": "targetLocationId",
        "required": True,
        "description": "Hub location (goods in or staging) the devices are received into.",
    }
    for import_type in by_type:
        link = await client.get(f"/api/v1/imports/templates/{import_type}/sample", headers=auth)
        assert link.status_code == 200, link.text
        assert link.json()["filename"] == f"{import_type}.csv"
    generated = await client.post(
        "/api/v1/imports/generate-sample",
        json={"type": "service_requests", "rows": 5, "withErrors": False},
        headers=auth,
    )
    assert generated.status_code == 200
    assert generated.json()["contentType"] == "text/csv"
    bad = await client.post(
        "/api/v1/imports/generate-sample", json={"type": "contacts", "rows": 0}, headers=auth
    )
    assert bad.status_code == 422


@pytest.mark.parametrize(
    ("persona", "method", "path", "status"),
    [
        (THEO, "get", "/api/v1/imports", 403),
        (THEO, "get", "/api/v1/imports/templates", 403),
        (VIC, "get", "/api/v1/imports", 200),
        (VIC, "post", "/api/v1/imports/generate-sample", 403),
        (ADA, "get", "/api/v1/imports", 200),
    ],
)
async def test_import_permissions(
    client: AsyncClient, headers: Headers, persona: str, method: str, path: str, status: int
) -> None:
    kwargs: dict[str, Any] = {"headers": await headers(persona)}
    if method == "post":
        kwargs["json"] = {"type": "contacts", "rows": 1, "withErrors": False}
    response = await getattr(client, method)(path, **kwargs)
    assert response.status_code == status


async def test_upload_forbidden_for_viewer_and_unauthenticated(
    client: AsyncClient, upload: Any
) -> None:
    response = await upload(srs(sr_row("SR0098501")), persona=VIC)
    assert response.status_code == 403
    anonymous = await client.get("/api/v1/imports")
    assert anonymous.status_code == 401


def test_import_summary_template_renders() -> None:
    template = templates.get("import.summary")
    mail = render("import.summary", template.sample_context)
    assert mail.subject == "Import #57 (service_requests) finished with 4 errors"
    assert "Rows with errors" in mail.text
    assert "http://localhost:5173/data/imports/57" in mail.html
