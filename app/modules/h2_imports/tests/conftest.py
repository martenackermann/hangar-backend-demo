"""H2 test helpers: CSV builders, upload + synchronous processing through SessionRuntime."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.h2_imports.importing.processor import ImportResult, run_import
from app.modules.h2_imports.importing.runtime import SessionRuntime

SR_HEADER = [
    "sr_number",
    "task_number",
    "itsm_state",
    "assignment_group",
    "opened_at",
    "user_id",
    "catalog_item",
    "short_description",
    "order_details",
    "delivery_address",
    "gender",
    "first_name",
    "last_name",
    "email",
    "cost_center",
]


def csv_bytes(header: list[str], rows: list[dict[str, Any]], *, delimiter: str = ",") -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, delimiter=delimiter, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in header})
    return buffer.getvalue().encode("utf-8")


def packed(**pairs: str) -> str:
    return ",".join(f"[{k.replace('_', ' ')}: {v}]" for k, v in pairs.items())


def order_details(
    procurement: str = "Replacement",
    new_base: str | None = "Laptop Standard 14",
    old_base: str | None = None,
    **extra: str,
) -> str:
    pairs = [("Procurement type", procurement)]
    if old_base:
        pairs.append(("Old base device", old_base))
    if new_base:
        pairs.append(("New base device type", new_base))
    pairs += [(k.replace("_", " "), v) for k, v in extra.items()]
    return ",".join(f"[{k}: {v}]" for k, v in pairs)


def address(street: str = "18 Harbour Road", zip_: str = "10110", city: str = "Riverton") -> str:
    return f"[Room: 1.01],[Building: B1],[Street: {street}],[Zip: {zip_}],[City: {city}],[Country: Freeland]"


def sr_row(number: str, user_id: str = "T810001", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "sr_number": number,
        "task_number": "TASK" + number[2:],
        "itsm_state": "Open",
        "assignment_group": "Workplace Rollout",
        "opened_at": "2026-10-12T08:14:00Z",
        "user_id": user_id,
        "catalog_item": "Workplace hardware replacement",
        "short_description": "Replace laptop",
        "order_details": order_details(),
        "delivery_address": address(),
        "gender": "female",
        "first_name": "Mia",
        "last_name": "Tester",
        "email": "mia.tester@example.com",
        "cost_center": "CC-4410",
    }
    row.update(overrides)
    return row


type Uploader = Callable[..., Awaitable[Any]]


@pytest.fixture
def upload(client: AsyncClient, headers: Callable[[str], Awaitable[dict[str, str]]]) -> Uploader:
    async def _upload(
        content: bytes,
        *,
        filename: str = "export.csv",
        import_type: str | None = None,
        options: dict[str, Any] | None = None,
        persona: str = "dana.dispatcher@example.com",
    ) -> Any:
        data: dict[str, str] = {}
        if import_type:
            data["type"] = import_type
        if options is not None:
            data["options"] = json.dumps(options)
        return await client.post(
            "/api/v1/imports",
            files={"file": (filename, content, "text/csv")},
            data=data,
            headers=await headers(persona),
        )

    return _upload


@pytest.fixture
def process(db_session: AsyncSession) -> Callable[[int], Awaitable[ImportResult]]:
    async def _process(import_job_id: int) -> ImportResult:
        return await run_import(SessionRuntime(db_session), import_job_id)

    return _process


@pytest.fixture
def run_file(
    upload: Uploader, process: Callable[[int], Awaitable[ImportResult]]
) -> Callable[..., Awaitable[ImportResult]]:
    """Upload ``content`` as Dana and process it synchronously."""

    async def _run(content: bytes, **kwargs: Any) -> ImportResult:
        response = await upload(content, **kwargs)
        assert response.status_code == 202, response.text
        return await process(response.json()["importJobId"])

    return _run
