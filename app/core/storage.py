"""Local-disk file storage with random names, type/size checks and signed download URLs.

- Files live under ``STORAGE_DIR`` at ``<subdir>/<random>.<ext>``; the *key* is that
  relative path. Store the key (plus original name, type, size) on your model.
- Uploads are accepted by extension allowlist + magic-byte sniffing; the browser's
  ``Content-Type`` is ignored (Windows browsers send ``application/vnd.ms-excel`` for CSV).
- Downloads go through ``GET /api/v1/files/{token}``; the token is an HMAC-signed,
  expiring reference created by :meth:`LocalStorage.signed_url`. Authorize *before*
  handing out a URL — the token itself is the credential.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

import orjson
from fastapi import Depends, UploadFile

from app.core.config import get_settings
from app.core.errors import DomainValidationError, GoneError, NotFoundError

CHUNK_SIZE = 1024 * 1024

# Extension -> canonical content type. Combine presets with ``|``.
CSV: Mapping[str, str] = {".csv": "text/csv", ".txt": "text/plain"}
XLSX: Mapping[str, str] = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
}
PDF: Mapping[str, str] = {".pdf": "application/pdf"}
IMAGES: Mapping[str, str] = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
SPREADSHEETS: Mapping[str, str] = {**CSV, **XLSX}

_MAGIC: Mapping[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
}
_TEXT_TYPES = {"text/csv", "text/plain"}
_KEY_RE = re.compile(r"^(?:[a-z0-9_-]+/)*[A-Za-z0-9_-]+\.[a-z0-9]+$")
_SUBDIR_RE = re.compile(r"^(?:[a-z0-9_-]+(?:/[a-z0-9_-]+)*)?$")


@dataclass(frozen=True, slots=True)
class StoredFile:
    key: str
    original_name: str
    content_type: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class FileGrant:
    """Decoded download token."""

    key: str
    filename: str
    content_type: str
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _safe_filename(name: str) -> str:
    base = PurePosixPath(name.replace("\\", "/")).name
    cleaned = re.sub(r"[^\w.\- ()]+", "_", base).strip(" .")
    return cleaned[:150] or "file"


def _looks_like_text(head: bytes) -> bool:
    return b"\x00" not in head


class LocalStorage:
    def __init__(self, root: Path, *, signing_key: bytes, base_url: str) -> None:
        self.root = root.resolve()
        self._signing_key = signing_key
        self._base_url = base_url.rstrip("/")

    # --- paths ---------------------------------------------------------------------

    def path_for(self, key: str) -> Path:
        if not _KEY_RE.match(key):
            raise NotFoundError("File not found.")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise NotFoundError("File not found.")
        return path

    def _new_key(self, subdir: str, ext: str) -> str:
        if not _SUBDIR_RE.match(subdir):
            raise ValueError(f"Invalid storage subdir: {subdir!r}")
        name = f"{secrets.token_hex(16)}{ext}"
        return f"{subdir}/{name}" if subdir else name

    # --- writes --------------------------------------------------------------------

    async def save_upload(
        self,
        upload: UploadFile,
        *,
        allowed: Mapping[str, str],
        field: str = "file",
        subdir: str = "uploads",
        max_bytes: int | None = None,
    ) -> StoredFile:
        """Validate and persist an uploaded file; 422 ``{field: [...]}`` on rejection."""
        limit = max_bytes if max_bytes is not None else get_settings().storage_max_upload_bytes
        original = _safe_filename(upload.filename or "upload")
        ext = PurePosixPath(original).suffix.lower()
        if ext not in allowed:
            raise DomainValidationError(
                {
                    field: f"File type '{ext or 'none'}' is not allowed. Allowed: {', '.join(sorted(allowed))}."
                }
            )
        content_type = allowed[ext]

        key = self._new_key(subdir, ext)
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        head = b""
        try:
            with path.open("wb") as out:
                while chunk := await upload.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > limit:
                        raise DomainValidationError(
                            {field: f"File is larger than {limit // (1024 * 1024)} MB."}
                        )
                    if len(head) < 512:
                        head += chunk[: 512 - len(head)]
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise DomainValidationError({field: "File is empty."})
            self._check_content(head, content_type, field)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return StoredFile(key, original, content_type, size, digest.hexdigest())

    def save_bytes(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str,
        subdir: str = "generated",
    ) -> StoredFile:
        """Persist generated content (PDF labels, XLSX exports, reports)."""
        original = _safe_filename(filename)
        key = self._new_key(subdir, PurePosixPath(original).suffix.lower() or ".bin")
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredFile(key, original, content_type, len(data), hashlib.sha256(data).hexdigest())

    @staticmethod
    def _check_content(head: bytes, content_type: str, field: str) -> None:
        if content_type in _TEXT_TYPES:
            ok = _looks_like_text(head)
        else:
            ok = any(head.startswith(magic) for magic in _MAGIC.get(content_type, (b"",)))
        if not ok:
            raise DomainValidationError({field: "File content does not match its file type."})

    # --- reads / deletes -----------------------------------------------------------

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def read_bytes(self, key: str) -> bytes:
        path = self.path_for(key)
        if not path.is_file():
            raise NotFoundError("File not found.")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        self.path_for(key).unlink(missing_ok=True)

    # --- signed URLs ---------------------------------------------------------------

    def _signature(self, payload: str) -> str:
        return _b64(hmac.new(self._signing_key, payload.encode(), hashlib.sha256).digest())

    def sign(
        self,
        key: str,
        *,
        filename: str,
        content_type: str = "application/octet-stream",
        ttl_seconds: int | None = None,
    ) -> str:
        self.path_for(key)  # validates the key format
        ttl = ttl_seconds if ttl_seconds is not None else get_settings().file_url_ttl_seconds
        claims: dict[str, Any] = {
            "k": key,
            "n": _safe_filename(filename),
            "t": content_type,
            "e": int(time.time()) + ttl,
        }
        payload = _b64(orjson.dumps(claims))
        return f"{payload}.{self._signature(payload)}"

    def verify(self, token: str) -> FileGrant:
        """Decode a token: 404 when tampered/malformed, 410 when expired."""
        payload, _, signature = token.partition(".")
        if (
            not payload
            or not signature
            or not hmac.compare_digest(signature, self._signature(payload))
        ):
            raise NotFoundError("File not found.")
        try:
            claims = orjson.loads(_unb64(payload))
            grant = FileGrant(
                key=str(claims["k"]),
                filename=str(claims["n"]),
                content_type=str(claims["t"]),
                expires_at=int(claims["e"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise NotFoundError("File not found.") from exc
        if grant.expires_at < int(time.time()):
            raise GoneError("This download link has expired.")
        return grant

    def signed_url(
        self,
        stored: StoredFile | str,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        ttl_seconds: int | None = None,
        absolute: bool = False,
    ) -> str:
        """URL for ``GET /api/v1/files/{token}``; relative unless ``absolute`` (e.g. mails)."""
        if isinstance(stored, StoredFile):
            key = stored.key
            filename = filename or stored.original_name
            content_type = content_type or stored.content_type
        else:
            key = stored
        token = self.sign(
            key,
            filename=filename or PurePosixPath(key).name,
            content_type=content_type or "application/octet-stream",
            ttl_seconds=ttl_seconds,
        )
        path = f"/api/v1/files/{token}"
        return f"{self._base_url}{path}" if absolute else path


@lru_cache
def get_storage() -> LocalStorage:
    settings = get_settings()
    signing_key = hmac.new(
        settings.jwt_secret.get_secret_value().encode(), b"hangar-storage-v1", hashlib.sha256
    ).digest()
    return LocalStorage(settings.storage_dir, signing_key=signing_key, base_url=settings.app_url)


StorageDep = Annotated[LocalStorage, Depends(get_storage)]
