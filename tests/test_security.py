"""Release security review: authentication on every route, demo gating, login throttling,
JWT handling, settings hardening, request/upload limits, export and PDF injection."""

from __future__ import annotations

import io
import re
import time
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import jwt
import pytest
import xlsxwriter
from click.testing import CliRunner
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import demo as demo_module
from app.core.auth import create_access_token, hash_password
from app.core.config import DEV_JWT_SECRET, DEV_SIGNING_KEY, Settings, get_settings
from app.core.enums import Role
from app.core.errors import ApiError, ConflictError, install_error_handlers
from app.core.files import (
    LocalFileStore,
    check_xlsx_archive,
    safe_filename,
    signed_url,
    store_file,
)
from app.core.jobs import public_error
from app.core.middleware import BodySizeLimitMiddleware
from app.core.models import User
from app.core.ratelimit import SlidingWindowLimiter
from app.core.spreadsheets import safe_cell
from app.modules.h1_requests.exports import workbook
from app.modules.h2_imports.importing.issues import Issue
from app.modules.h2_imports.importing.report import build_error_report
from app.modules.h4_field.protocol import ProtocolData, render_protocol_pdf
from app.modules.h5_stock.services.common import csv_bytes
from app.modules.h5_stock.services.documents import DocLine, OrderDocument, order_pdf

DANA, THEO, VIC, ADA = (
    "dana.dispatcher@example.com",
    "theo.technician@example.com",
    "vic.viewer@example.com",
    "ada.admin@example.com",
)
Headers = Callable[[str], Awaitable[dict[str, str]]]
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Operations that work without a bearer token (SPEC §14 "Security").
PUBLIC = {
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/personas"),
    ("POST", "/api/v1/auth/persona-login"),
    ("GET", "/api/v1/meta/config"),
    ("GET", "/api/v1/files/{file_id}/download"),
    ("GET", "/api/v1/healthz"),
    ("GET", "/api/v1/readyz"),
}


def _operations(app: FastAPI) -> list[tuple[str, str]]:
    return [
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    ]


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


async def _make_user(
    session: AsyncSession,
    email: str,
    role: Role,
    *,
    active: bool = True,
    password: str = "pw",  # noqa: S107
) -> User:
    user = User(
        email=email,
        first_name="Rel",
        last_name="Ease",
        role=role,
        password_hash=hash_password(password),
        is_active=active,
        is_demo_persona=False,
    )
    session.add(user)
    await session.flush()
    return user


def _bearer(user: User) -> dict[str, str]:
    token, _ = create_access_token(user)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def demo_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "demo_mode", False)


# --- authentication + authorization sweeps ---------------------------------------------------


async def test_every_non_public_operation_requires_a_token(
    app: FastAPI, client: AsyncClient
) -> None:
    operations = _operations(app)
    assert len(operations) > 150
    assert set(operations) >= PUBLIC
    wrong: list[str] = []
    for method, path in operations:
        if (method, path) in PUBLIC:
            continue
        response = await client.request(method, _concrete(path))
        if response.status_code != 401:
            wrong.append(f"{method} {path} -> {response.status_code}")
    assert wrong == []


async def test_viewer_cannot_change_anything(
    app: FastAPI, client: AsyncClient, headers: Headers
) -> None:
    """Read-only role: no mutating operation succeeds and none crashes."""
    auth = await headers(VIC)
    allowed = {("POST", "/api/v1/auth/logout"), ("POST", "/api/v1/region-rules/test")}
    succeeded: list[str] = []
    crashed: list[str] = []
    for method, path in _operations(app):
        if method == "GET" or (method, path) in PUBLIC:
            continue
        response = await client.request(method, _concrete(path), json={}, headers=auth)
        if response.status_code >= 500:
            crashed.append(f"{method} {path} -> {response.status_code}")
        elif response.status_code < 300 and (method, path) not in allowed:
            succeeded.append(f"{method} {path} -> {response.status_code}")
    assert (succeeded, crashed) == ([], [])


async def test_security_headers(client: AsyncClient) -> None:
    response = await client.get("/api/v1/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])
    spoofed = await client.get("/api/v1/healthz", headers={"X-Request-Id": "a\tb<script>"})
    assert spoofed.headers["x-request-id"] != "a\tb<script>"


# --- demo-only endpoints -------------------------------------------------------------------


@pytest.mark.usefixtures("demo_off")
async def test_demo_endpoints_are_gone_outside_demo_mode(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _make_user(db_session, "release.admin@example.com", Role.ADMIN)
    dispatcher = await _make_user(db_session, "release.dispatcher@example.com", Role.DISPATCHER)
    assert (await client.get("/api/v1/auth/personas")).status_code == 404
    persona = await client.post("/api/v1/auth/persona-login", json={"email": DANA})
    assert persona.status_code == 404
    reset = await client.post("/api/v1/demo/reset", headers=_bearer(admin))
    assert reset.status_code == 404
    sample = await client.post(
        "/api/v1/imports/generate-sample",
        json={"type": "service_requests", "rows": 5},
        headers=_bearer(dispatcher),
    )
    assert sample.status_code == 404
    config = (await client.get("/api/v1/meta/config")).json()
    assert (config["demoMode"], config["mailpitUrl"]) == (False, None)


async def test_demo_personas_are_locked_out_outside_demo_mode(
    client: AsyncClient, db_session: AsyncSession, headers: Headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    persona_token = await headers(DANA)
    await _make_user(db_session, "release.user@example.com", Role.DISPATCHER, password="s3cret!")
    monkeypatch.setattr(get_settings(), "demo_mode", False)
    password = await client.post("/api/v1/auth/login", json={"email": DANA, "password": "demo1234"})
    assert (password.status_code, password.json()["code"]) == (401, "invalid_credentials")
    assert (await client.get("/api/v1/me", headers=persona_token)).status_code == 401
    real = await client.post(
        "/api/v1/auth/login", json={"email": "release.user@example.com", "password": "s3cret!"}
    )
    assert real.status_code == 200


async def test_demo_reset_task_and_cli_refuse_outside_demo_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import cli as cli_module

    ran: list[str] = []

    async def fake_reset(job_id: int | None = None) -> None:
        ran.append("reset")

    def fake_seed(*, reset: bool, export: Any) -> None:
        ran.append("seed")

    monkeypatch.setattr(demo_module, "run_demo_reset", fake_reset)
    monkeypatch.setattr(cli_module, "_seed", fake_seed)
    settings = get_settings()
    monkeypatch.setattr(settings, "demo_mode", False)
    await demo_module.demo_reset_task(None)
    monkeypatch.setattr(settings, "environment", "production")
    for command in (["seed"], ["seed", "--reset"], ["demo-reset"]):
        result = CliRunner().invoke(cli_module.cli, command)
        assert result.exit_code != 0
        assert "Refusing to load demo data" in result.output
    assert ran == []
    monkeypatch.setattr(settings, "demo_mode", True)
    await demo_module.demo_reset_task(None)
    assert CliRunner().invoke(cli_module.cli, ["seed"]).exit_code == 0
    assert ran == ["reset", "seed"]


async def test_demo_personas_stay_active(
    client: AsyncClient, headers: Headers, user: Callable[[str], Awaitable[User]]
) -> None:
    theo = await user(THEO)
    response = await client.post(
        f"/api/v1/users/{theo.id}/actions/deactivate", json={}, headers=await headers(ADA)
    )
    assert response.status_code == 409


# --- login ---------------------------------------------------------------------------------


async def test_failed_logins_are_throttled_per_account(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_account", 3)
    for _ in range(3):
        bad = await client.post("/api/v1/auth/login", json={"email": DANA, "password": "nope"})
        assert bad.status_code == 401
    blocked = await client.post("/api/v1/auth/login", json={"email": DANA, "password": "demo1234"})
    assert (blocked.status_code, blocked.json()["code"]) == (429, "too_many_requests")
    assert int(blocked.headers["retry-after"]) > 0
    other = await client.post("/api/v1/auth/login", json={"email": VIC, "password": "demo1234"})
    assert other.status_code == 200


async def test_failed_logins_are_throttled_per_ip(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_ip", 4)
    for index in range(4):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": f"nobody{index}@example.com", "password": "x"},
        )
        assert response.status_code == 401
    blocked = await client.post("/api/v1/auth/login", json={"email": VIC, "password": "demo1234"})
    assert blocked.status_code == 429


async def test_successful_login_clears_the_account_counter(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "login_rate_limit_per_account", 2)
    for _ in range(3):
        await client.post("/api/v1/auth/login", json={"email": DANA, "password": "nope"})
        ok = await client.post("/api/v1/auth/login", json={"email": DANA, "password": "demo1234"})
        assert ok.status_code == 200


async def test_persona_logins_are_throttled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "persona_login_rate_limit_per_ip", 2)
    for _ in range(2):
        ok = await client.post("/api/v1/auth/persona-login", json={"email": THEO})
        assert ok.status_code == 200
    blocked = await client.post("/api/v1/auth/persona-login", json={"email": THEO})
    assert blocked.status_code == 429


def test_sliding_window_limiter() -> None:
    now = [1000.0]
    limiter = SlidingWindowLimiter(max_keys=2, clock=lambda: now[0])
    for _ in range(3):
        limiter.hit("a", window=60)
    assert limiter.retry_after("a", limit=3, window=60) == 60
    assert limiter.retry_after("a", limit=4, window=60) is None
    now[0] += 61
    assert limiter.retry_after("a", limit=3, window=60) is None
    for key in ("b", "c", "d"):
        limiter.hit(key, window=60)
    assert len(limiter._hits) <= 2  # bounded memory


async def test_jwt_validation(
    client: AsyncClient, db_session: AsyncSession, user: Callable[[str], Awaitable[User]]
) -> None:
    dana = await user(DANA)
    secret = get_settings().jwt_secret.get_secret_value()
    now = datetime.now(UTC)
    claims = {
        "sub": str(dana.id),
        "role": "dispatcher",
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    tokens = {
        "alg none": jwt.encode(claims, None, algorithm="none"),
        "other secret": jwt.encode(claims, "x" * 40, algorithm="HS256"),
        "other algorithm": jwt.encode(claims, secret * 2, algorithm="HS512"),
        "expired": jwt.encode(
            {**claims, "exp": now - timedelta(seconds=5)}, secret, algorithm="HS256"
        ),
        "no exp": jwt.encode(
            {k: v for k, v in claims.items() if k != "exp"}, secret, algorithm="HS256"
        ),
        "unknown user": jwt.encode({**claims, "sub": "999999"}, secret, algorithm="HS256"),
    }
    for name, token in tokens.items():
        response = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, name
    valid = jwt.encode(claims, secret, algorithm="HS256")
    ok = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {valid}"})
    assert ok.status_code == 200
    inactive = await _make_user(db_session, "release.gone@example.com", Role.ADMIN, active=False)
    gone = await client.get("/api/v1/me", headers=_bearer(inactive))
    assert (gone.status_code, gone.json()["code"]) == (403, "account_inactive")


# --- settings ------------------------------------------------------------------------------

STRONG = "q7Vb2kP9xL0sR4tY8wE3nM6zA1cD5fG7hJ2"


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_production_refuses_placeholder_secrets() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _settings(environment="production", jwt_secret=DEV_JWT_SECRET, file_url_signing_key=STRONG)
    with pytest.raises(ValidationError, match="FILE_URL_SIGNING_KEY"):
        _settings(environment="production", jwt_secret=STRONG, file_url_signing_key=DEV_SIGNING_KEY)
    with pytest.raises(ValidationError, match="FILE_URL_SIGNING_KEY"):
        _settings(environment="production", jwt_secret=STRONG, file_url_signing_key="short")
    compose = {
        "jwt_secret": "demo-only-jwt-secret-change-me-0123456789",
        "file_url_signing_key": "demo-only-file-signing-key-change-me",
    }
    # docker compose: production + demo mode with the documented demo secrets keeps working
    assert _settings(environment="production", demo_mode=True, **compose).is_production
    with pytest.raises(ValidationError, match="placeholder"):
        _settings(environment="production", demo_mode=False, **compose)
    real = _settings(
        environment="production", demo_mode=False, jwt_secret=STRONG, file_url_signing_key=STRONG
    )
    assert real.jwt_algorithm == "HS256"
    # development keeps its zero-config defaults
    assert _settings(environment="development").file_url_signing_key.get_secret_value()


def test_settings_reject_wildcard_cors_and_weak_jwt_algorithms() -> None:
    with pytest.raises(ValidationError, match="explicit origins"):
        _settings(cors_origins="*")
    with pytest.raises(ValidationError):
        _settings(jwt_algorithm="none")
    with pytest.raises(ValidationError):
        _settings(jwt_algorithm="RS256")
    assert _settings(cors_origins="https://app.example.com/").cors_origins == [
        "https://app.example.com"
    ]


# --- request size --------------------------------------------------------------------------


async def test_oversized_json_body_is_rejected(client: AsyncClient, headers: Headers) -> None:
    auth = await headers(VIC)
    big = b'{"route": "/x", "name": "n", "search": {"q": "' + b"x" * (3 * 1024 * 1024) + b'"}}'
    declared = await client.post(
        "/api/v1/saved-views",
        content=big,
        headers={**auth, "Content-Type": "application/json"},
    )
    assert (declared.status_code, declared.json()["code"]) == (413, "too_large")
    assert declared.headers["content-type"] == "application/problem+json"

    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(big), 256 * 1024):
            yield big[start : start + 256 * 1024]

    streamed = await client.post(
        "/api/v1/saved-views",
        content=chunks(),
        headers={**auth, "Content-Type": "application/json"},
    )
    assert (streamed.status_code, streamed.json()["code"]) == (413, "too_large")


async def test_multipart_limit_applies_while_streaming() -> None:
    probe = FastAPI()
    install_error_handlers(probe)

    @probe.post("/upload")
    async def upload(request: Request) -> dict[str, int]:
        form = await request.form()
        return {"fields": len(form)}

    probe.add_middleware(BodySizeLimitMiddleware, max_bytes=1024, upload_bytes=0)
    payload = b"z" * (1024 * 1024 + 4096)  # over upload_bytes + 1 MiB of form overhead
    async with AsyncClient(transport=ASGITransport(app=probe), base_url="http://t") as c:
        small = await c.post("/upload", files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")})
        assert small.status_code == 200
        big = await c.post("/upload", files={"file": ("a.csv", payload, "text/csv")})
        assert big.status_code == 413

        async def chunks() -> AsyncIterator[bytes]:
            boundary = b"--b0undary\r\n"
            yield (
                boundary + b'Content-Disposition: form-data; name="file"; filename="a.csv"\r\n\r\n'
            )
            for _ in range(20):
                yield b"y" * 65536
            yield b"\r\n--b0undary--\r\n"

        streamed = await c.post(
            "/upload",
            content=chunks(),
            headers={"Content-Type": "multipart/form-data; boundary=b0undary"},
        )
        assert streamed.status_code == 413


# --- uploads -------------------------------------------------------------------------------


def _xlsx(rows: list[list[str]]) -> bytes:
    buffer = io.BytesIO()
    book = xlsxwriter.Workbook(buffer, {"in_memory": True})
    sheet = book.add_worksheet()
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            sheet.write_string(r, c, value)
    book.close()
    return buffer.getvalue()


def _zip_bomb(size: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/worksheets/sheet1.xml", b"\0" * size)
    return buffer.getvalue()


def test_xlsx_archive_check() -> None:
    check_xlsx_archive(_xlsx([["serial_number"], ["ABC123"]]), max_uncompressed=1_000_000)
    bomb = _zip_bomb(2_000_000)
    assert len(bomb) < 20_000
    with pytest.raises(ApiError) as too_large:
        check_xlsx_archive(bomb, max_uncompressed=1_000_000)
    assert (too_large.value.status, too_large.value.code) == (413, "too_large")
    with pytest.raises(ApiError) as entries:
        check_xlsx_archive(_xlsx([["a"]]), max_entries=2)
    assert entries.value.status == 413
    with pytest.raises(ApiError) as broken:
        check_xlsx_archive(b"PK\x03\x04" + b"garbage" * 50)
    assert (broken.value.status, broken.value.code) == (400, "invalid_file")


async def test_xlsx_upload_rejects_zip_bombs(
    client: AsyncClient, headers: Headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "xlsx_max_uncompressed_bytes", 1_000_000)
    auth = await headers(DANA)
    bomb = await client.post(
        "/api/v1/imports/detect",
        files={"file": ("bomb.xlsx", _zip_bomb(5_000_000), XLSX_TYPE)},
        headers=auth,
    )
    assert (bomb.status_code, bomb.json()["code"]) == (413, "too_large")
    deactivation = await client.post(
        "/api/v1/deactivations",
        files={"file": ("bomb.xlsx", _zip_bomb(5_000_000), XLSX_TYPE)},
        data={"reason": "disposed"},
        headers=auth,
    )
    assert deactivation.status_code == 413
    fine = await client.post(
        "/api/v1/imports/detect",
        files={"file": ("ok.xlsx", _xlsx([["user_id", "last_name"], ["T1", "X"]]), XLSX_TYPE)},
        headers=auth,
    )
    assert fine.status_code == 200


def test_file_names_and_store_paths(tmp_path: Any) -> None:
    assert safe_filename('..\\..\\evil"\r\nX-Injected: 1.pdf') == "evil_X-Injected_ 1.pdf"
    assert safe_filename("../../etc/passwd") == "passwd"
    store = LocalFileStore(tmp_path)
    with pytest.raises(ApiError):
        store.path("../outside.txt")


async def test_download_headers_cannot_be_injected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    stored = await store_file(
        db_session, b"%PDF-1.7", 'a"\r\nSet-Cookie: x=1.pdf', "application/pdf", "label"
    )
    link = signed_url(stored)
    response = await client.get("/api/v1" + link.url.split("/api/v1", 1)[1])
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert "\n" not in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"
    expires = int(time.time()) + 3600
    forged = await client.get(
        f"/api/v1/files/{stored.id}/download",
        params={"expires": expires, "signature": "0" * 64},
    )
    assert forged.status_code == 403


# --- exports + documents -------------------------------------------------------------------


def test_safe_cell() -> None:
    for value in ("=1+1", "+cmd|' /C calc'!A0", "-2+3", "@SUM(A1)", "\tx", "\rx", "=HYPERLINK(1)"):
        assert safe_cell(value) == "'" + value
    for value in ("plain", "-5", "+41 44 123 45 67", "-1.5", "", "a=b"):
        assert safe_cell(value) == value


def test_csv_exports_neutralise_formulas() -> None:
    data = csv_bytes(("a", "b", "c", "d"), [["=cmd()", -5, "+41 44 1", date(2026, 1, 2)]])
    assert data.decode("utf-8-sig").splitlines()[1] == "'=cmd(),-5,+41 44 1,2026-01-02"
    report = build_error_report(
        ["user_id", "last_name"],
        {2: ['=HYPERLINK("http://x")', "@evil"]},
        [(2, Issue("error", "user_id", "invalid_format", "Bad value"))],
    ).decode("utf-8-sig")
    assert "'=HYPERLINK" in report
    assert ",'@evil," in report


def test_xlsx_exports_write_text_as_text() -> None:
    data = workbook(
        "Export",
        (("Name", 10), ("Link", 10), ("Count", 5)),
        [['=HYPERLINK("http://evil.example.com","x")', "http://example.com", 3]],
    )
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        names = archive.namelist()
    assert "<f>" not in sheet
    assert "hyperlink" not in sheet.lower()
    assert not any("sheet1.xml.rels" in n for n in names)


def test_pdf_paragraphs_escape_user_text() -> None:
    hostile = '<img src="/etc/passwd" width="10" height="10"/><a href="http://x">&'
    pdf = render_protocol_pdf(
        ProtocolData(
            sr_number="SR0012345",
            task_number=None,
            user_name="Test Person",
            user_id="T900001",
            completed_at=datetime(2026, 10, 13, 9, 0, tzinfo=UTC),
            recipient_name="Test Person",
            technician_name="Theo Technician",
            os_reinstall_on_pickup=False,
            software=[hostile],
            deviation=("DV01", "Device <broken", hostile),
        )
    )
    assert pdf.startswith(b"%PDF")
    order = order_pdf(
        OrderDocument(
            number="CO-1",
            status_label="Draft",
            site_name="Riverton",
            site_address="1 Main Street",
            delivery_date=date(2026, 10, 20),
            recipient_email="partner-orders@example.com",
            note=hostile,
            created_by="Dana Dispatcher",
            created_at=datetime(2026, 10, 13, 9, 0, tzinfo=UTC),
            sent_at=None,
            lines=[DocLine("A-1", None, hostile, "M1", 1, 1, 0, 0)],
        )
    )
    assert order.startswith(b"%PDF")


# --- jobs + search -------------------------------------------------------------------------


def test_job_errors_do_not_leak_internals(monkeypatch: pytest.MonkeyPatch) -> None:
    sql = RuntimeError("(psycopg.errors.X) boom\n[SQL: SELECT secret FROM users]\n[parameters: 1]")
    assert public_error(sql) == "RuntimeError: (psycopg.errors.X) boom"
    assert public_error(ConflictError("Nothing to do.")) == "Nothing to do."
    monkeypatch.setattr(get_settings(), "environment", "production")
    assert "SELECT" not in public_error(sql)
    assert public_error(sql).startswith("RuntimeError:")


async def test_search_drops_hits_the_caller_cannot_read(
    client: AsyncClient, headers: Headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import search as search_module

    params = {"q": "SR0012001"}
    visible = (await client.get("/api/v1/search", params=params, headers=await headers(VIC))).json()
    assert visible["exact"]["kind"] == "service_request"
    monkeypatch.setitem(search_module.KIND_PERMISSIONS, "service_request", "master.manage")
    hidden = (await client.get("/api/v1/search", params=params, headers=await headers(VIC))).json()
    assert all(group["kind"] != "service_request" for group in hidden["groups"])
    assert hidden["exact"] is None or hidden["exact"]["kind"] != "service_request"
