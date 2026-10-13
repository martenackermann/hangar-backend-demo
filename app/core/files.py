"""File store + signed download URLs (SPEC §2.4 "Files", API.md §1.6 / §3.7).

- Bytes live on local disk under ``HANGAR_FILES_DIR`` (``LocalFileStore``); metadata in
  ``stored_files``. Keys are random: ``<purpose>/<yyyy>/<mm>/<uuid><ext>``.
- ``signed_url(file)`` → :class:`DownloadLink` pointing at
  ``{HANGAR_PUBLIC_API_URL}/files/{id}/download?expires=<unix>&signature=<hex>``
  (HMAC-SHA256 over ``"{id}:{expires}"`` with ``FILE_URL_SIGNING_KEY``; default TTL 10 min).
- ``read_upload`` validates multipart uploads (extension + magic bytes + size; XLSX archives
  are checked for zip bombs before any parser sees them).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import re
import time
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Protocol

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError, BadRequestError, ForbiddenError, NotFoundError
from app.core.models import StoredFile, User
from app.core.schemas import DownloadLink, EntityRef

CSV: Mapping[str, str] = {".csv": "text/csv"}
XLSX: Mapping[str, str] = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
}
PDF: Mapping[str, str] = {".pdf": "application/pdf"}
SPREADSHEETS: Mapping[str, str] = {**CSV, **XLSX}

_MAGIC: Mapping[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
}
_SAFE_NAME = re.compile(r"[^\w.\- ()]+")
_XLSX_TYPE = XLSX[".xlsx"]


# --- storage backend -----------------------------------------------------------------------


class FileStore(Protocol):
    def write(self, key: str, data: bytes) -> None: ...
    def read(self, key: str) -> bytes: ...
    def path(self, key: str) -> Path: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...


class LocalFileStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if not target.is_relative_to(self.root):
            raise NotFoundError("File not found.")
        return target

    def write(self, key: str, data: bytes) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def read(self, key: str) -> bytes:
        target = self.path(key)
        if not target.is_file():
            raise NotFoundError("File not found.")
        return target.read_bytes()

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()


_store_override: FileStore | None = None


@lru_cache
def _default_store() -> LocalFileStore:
    return LocalFileStore(get_settings().files_dir)


def file_store() -> FileStore:
    return _store_override or _default_store()


def set_file_store(store: FileStore | None) -> None:
    """Replace the backend (tests use a temp directory)."""
    global _store_override
    _store_override = store


# --- metadata + bytes ----------------------------------------------------------------------


def safe_filename(name: str) -> str:
    base = PurePosixPath(name.replace("\\", "/")).name
    cleaned = _SAFE_NAME.sub("_", base).strip(" .")
    return cleaned[:200] or "file"


async def store_file(
    session: AsyncSession,
    data: bytes,
    filename: str,
    content_type: str,
    purpose: str,
    entity: EntityRef | tuple[str, int] | None = None,
    ttl: timedelta | None = None,
    created_by: User | int | None = None,
    created_at: datetime | None = None,
) -> StoredFile:
    """Write ``data`` to the store and insert its ``stored_files`` row (flushed, not committed).

    If the transaction rolls back the bytes stay on disk until ``files.cleanup`` — harmless.
    ``created_at`` backdates the row (seeders telling a story in the past).
    """
    name = safe_filename(filename)
    now = datetime.now(UTC)
    ext = PurePosixPath(name).suffix.lower()[:10]
    key = f"{purpose}/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{ext}"
    file_store().write(key, data)
    entity_type, entity_id = (
        (entity.type, entity.id) if isinstance(entity, EntityRef) else entity or (None, None)
    )
    row = StoredFile(
        storage_key=key,
        filename=name,
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        purpose=purpose,
        entity_type=entity_type,
        entity_id=entity_id,
        created_by=created_by.id if isinstance(created_by, User) else created_by,
        expires_at=now + ttl if ttl is not None else None,
    )
    if created_at is not None:
        row.created_at = row.updated_at = created_at
    session.add(row)
    await session.flush()
    return row


def read_file(file: StoredFile) -> bytes:
    return file_store().read(file.storage_key)


# --- signed URLs ---------------------------------------------------------------------------


def _signature(file_id: int, expires: int) -> str:
    key = get_settings().file_url_signing_key.get_secret_value().encode()
    return hmac.new(key, f"{file_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def signed_url(file: StoredFile, ttl: timedelta | None = None) -> DownloadLink:
    settings = get_settings()
    lifetime = ttl if ttl is not None else timedelta(seconds=settings.file_url_ttl_seconds)
    expires = int(time.time() + lifetime.total_seconds())
    url = (
        f"{settings.public_api_url}/files/{file.id}/download"
        f"?expires={expires}&signature={_signature(file.id, expires)}"
    )
    return DownloadLink(
        url=url,
        filename=file.filename,
        content_type=file.content_type,
        size_bytes=file.size_bytes,
        expires_at=datetime.fromtimestamp(expires, UTC),
    )


def verify_signature(file_id: int, expires: int, signature: str) -> None:
    """``403 invalid_signature`` for tampered or expired links."""
    if expires < int(time.time()) or not hmac.compare_digest(
        signature.lower(), _signature(file_id, expires)
    ):
        raise ForbiddenError(
            "This download link is invalid or has expired.", code="invalid_signature"
        )


# --- uploads -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Upload:
    data: bytes
    filename: str
    content_type: str
    extension: str


async def read_upload(
    upload: UploadFile, *, allowed: Mapping[str, str], max_bytes: int | None = None
) -> Upload:
    """Read and validate an upload: ``415 unsupported_media_type``, ``413 too_large``,
    ``400 empty_file`` / ``invalid_file`` (content does not match the extension)."""
    limit = max_bytes if max_bytes is not None else get_settings().import_max_bytes
    name = safe_filename(upload.filename or "upload")
    ext = PurePosixPath(name).suffix.lower()
    if ext not in allowed:
        raise ApiError(
            f"Unsupported file type '{ext or 'none'}'. Allowed: {', '.join(sorted(allowed))}.",
            status=415,
            code="unsupported_media_type",
        )
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise ApiError(
            f"The file is larger than {limit // (1024 * 1024)} MB.", status=413, code="too_large"
        )
    if not data:
        raise BadRequestError("The file is empty.", code="empty_file")
    content_type = allowed[ext]
    magic = _MAGIC.get(content_type)
    if magic and not any(data.startswith(m) for m in magic):
        raise BadRequestError("The file content does not match its type.", code="invalid_file")
    if content_type.startswith("text/") and b"\x00" in data[:4096]:
        raise BadRequestError("The file content does not match its type.", code="invalid_file")
    if content_type == _XLSX_TYPE:
        check_xlsx_archive(data)
    return Upload(data, name, content_type, ext)


def check_xlsx_archive(
    data: bytes, *, max_uncompressed: int | None = None, max_entries: int | None = None
) -> None:
    """Reject zip bombs before a spreadsheet parser inflates the workbook.

    Every member is actually decompressed (streamed, nothing kept) and counted, so lying
    size headers do not help: ``413 too_large`` past the limit, ``400 invalid_file`` for
    archives that are not readable zips (bad CRC, truncated, encrypted, ...).
    """
    settings = get_settings()
    limit = settings.xlsx_max_uncompressed_bytes if max_uncompressed is None else max_uncompressed
    entries = settings.xlsx_max_entries if max_entries is None else max_entries
    too_large = ApiError("The workbook is too large when unpacked.", status=413, code="too_large")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > entries or sum(m.file_size for m in members) > limit:
                raise too_large
            total = 0
            for member in members:
                with archive.open(member) as stream:
                    while chunk := stream.read(1024 * 1024):
                        total += len(chunk)
                        if total > limit:
                            raise too_large
    except ApiError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, ValueError, OSError) as exc:
        raise BadRequestError(
            "The file content does not match its type.", code="invalid_file"
        ) from exc
