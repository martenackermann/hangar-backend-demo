"""``GET /api/v1/files/{token}`` — download a stored file via a signed, expiring token."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from app.core.errors import NotFoundError, problem_responses
from app.core.storage import StorageDep

router = APIRouter(tags=["files"])


@router.get(
    "/files/{token}",
    response_class=FileResponse,
    summary="Download a file (signed URL)",
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "The file"},
        **problem_responses(404, 410),
    },
)
async def download_file(
    token: str,
    storage: StorageDep,
    inline: Annotated[bool, Query(description="Render inline instead of as attachment.")] = False,
) -> FileResponse:
    grant = storage.verify(token)
    path = storage.path_for(grant.key)
    if not path.is_file():
        raise NotFoundError("File not found.")
    return FileResponse(
        path,
        media_type=grant.content_type,
        filename=grant.filename,
        content_disposition_type="inline" if inline else "attachment",
        headers={"Cache-Control": "private, no-store"},
    )
