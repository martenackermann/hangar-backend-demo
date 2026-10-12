"""Local storage: upload validation, generated files and signed download URLs."""

import io
import time

import pytest
import time_machine
from fastapi import UploadFile
from httpx import AsyncClient
from starlette.datastructures import Headers

from app.core.errors import DomainValidationError, GoneError, NotFoundError
from app.core.storage import CSV, PDF, XLSX, LocalStorage


def upload(name: str, data: bytes, content_type: str = "application/octet-stream") -> UploadFile:
    return UploadFile(
        io.BytesIO(data), filename=name, headers=Headers({"content-type": content_type})
    )


async def test_save_csv_upload_ignores_browser_content_type(storage: LocalStorage) -> None:
    stored = await storage.save_upload(
        upload("../../Import 2026.CSV", b"a;b\n1;2\n", "application/vnd.ms-excel"),
        allowed=CSV,
        subdir="imports",
    )
    assert stored.key.startswith("imports/")
    assert stored.key.endswith(".csv")
    assert stored.original_name == "Import 2026.CSV"
    assert stored.content_type == "text/csv"
    assert stored.size == 8
    assert storage.read_bytes(stored.key) == b"a;b\n1;2\n"
    assert storage.path_for(stored.key).is_relative_to(storage.root)


@pytest.mark.parametrize(
    ("name", "data", "allowed", "message"),
    [
        ("evil.exe", b"MZ...", CSV, "not allowed"),
        ("fake.pdf", b"hello", PDF, "does not match"),
        ("fake.xlsx", b"%PDF-1.7", XLSX, "does not match"),
        ("binary.csv", b"a,b\x00c", CSV, "does not match"),
        ("empty.csv", b"", CSV, "empty"),
    ],
)
async def test_upload_rejections(
    storage: LocalStorage, name: str, data: bytes, allowed: dict[str, str], message: str
) -> None:
    with pytest.raises(DomainValidationError) as caught:
        await storage.save_upload(upload(name, data), allowed=allowed, field="document")
    [error] = caught.value.extensions["errors"]["document"]
    assert message in error
    assert not any(p.is_file() for p in storage.root.rglob("*"))  # nothing left behind


async def test_upload_size_limit(storage: LocalStorage) -> None:
    with pytest.raises(DomainValidationError, match="One or more"):
        await storage.save_upload(upload("big.csv", b"x" * 2048), allowed=CSV, max_bytes=1024)


def test_path_traversal_is_rejected(storage: LocalStorage) -> None:
    for key in ("../secret.txt", "/etc/passwd", "a/../../b.csv", "C:/x.csv", "noext"):
        with pytest.raises(NotFoundError):
            storage.path_for(key)


def test_signed_token_roundtrip_tamper_and_expiry(storage: LocalStorage) -> None:
    stored = storage.save_bytes(
        b"%PDF-1.7 label", filename="label.pdf", content_type="application/pdf"
    )
    token = storage.sign(
        stored.key, filename="label SR1.pdf", content_type="application/pdf", ttl_seconds=60
    )

    grant = storage.verify(token)
    assert (grant.key, grant.filename, grant.content_type) == (
        stored.key,
        "label SR1.pdf",
        "application/pdf",
    )

    payload, _, signature = token.partition(".")
    with pytest.raises(NotFoundError):
        storage.verify(f"{payload}.{signature[:-2]}xx")
    with pytest.raises(NotFoundError):
        storage.verify("garbage")
    other = LocalStorage(storage.root, signing_key=b"another-key", base_url="http://test")
    with pytest.raises(NotFoundError):
        other.verify(token)

    with time_machine.travel(time.time() + 120, tick=False), pytest.raises(GoneError):
        storage.verify(token)


async def test_download_route(client: AsyncClient, storage: LocalStorage) -> None:
    stored = storage.save_bytes(
        b"%PDF-1.7 demo", filename="protocol.pdf", content_type="application/pdf"
    )
    url = storage.signed_url(stored)
    assert url.startswith("/api/v1/files/")
    assert storage.signed_url(stored, absolute=True).startswith("http://test/api/v1/files/")

    response = await client.get(url)
    assert response.status_code == 200
    assert response.content == b"%PDF-1.7 demo"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "protocol.pdf" in response.headers["content-disposition"]

    inline = await client.get(url, params={"inline": True})
    assert inline.headers["content-disposition"].startswith("inline;")

    storage.delete(stored.key)
    missing = await client.get(url)
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"


async def test_expired_download_is_410(client: AsyncClient, storage: LocalStorage) -> None:
    stored = storage.save_bytes(b"a,b\n", filename="export.csv", content_type="text/csv")
    url = storage.signed_url(stored, ttl_seconds=-1)
    response = await client.get(url)
    assert response.status_code == 410
    assert response.json()["detail"] == "This download link has expired."
