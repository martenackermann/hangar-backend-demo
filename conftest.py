"""Shared pytest fixtures for ``tests/`` and ``app/modules/*/tests/``.

- DB: ``TEST_DATABASE_URL`` (shell, else ``.env``; default ``hangar_test``). Once per session
  the schema is rebuilt (``create_all`` + ``pg_trgm`` + procrastinate schema), all slices are
  loaded and the **seed story is committed** — tests run on top of it inside a transaction
  that is rolled back (``db_session``), so ``commit()`` in code under test is safe.
- Jobs defer into an in-memory procrastinate connector (``jobs``), mail goes to a
  ``MemoryMailer`` (``mailer``), files to a temp directory.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from pathlib import Path


def _dotenv_value(name: str) -> str | None:
    """``name`` from the repo's ``.env`` (the shell wins), so a worktree's own test DB is used
    even when the variable is only set there."""
    env_file = Path(__file__).with_name(".env")
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == name and not line.lstrip().startswith("#"):
            return value.strip().strip("'\"") or None
    return None


os.environ["DATABASE_URL"] = (
    os.environ.get("TEST_DATABASE_URL")
    or _dotenv_value("TEST_DATABASE_URL")
    or "postgresql+psycopg://dev:dev@localhost:5432/hangar_test"
)
os.environ["ENVIRONMENT"] = "test"
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ["GATEWAY_MAILER"] = "memory"
_files_dir = tempfile.mkdtemp(prefix="hangar-test-files-")
os.environ["HANGAR_FILES_DIR"] = _files_dir
atexit.register(shutil.rmtree, _files_dir, ignore_errors=True)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from procrastinate.testing import InMemoryConnector  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402

from app.core import gateways  # noqa: E402
from app.core.auth import create_access_token  # noqa: E402
from app.core.db import create_engine, get_session, set_sessionmaker  # noqa: E402
from app.core.dbadmin import recreate_schema  # noqa: E402
from app.core.files import LocalFileStore, set_file_store  # noqa: E402
from app.core.gateways.mailers import MemoryMailer  # noqa: E402
from app.core.models import User  # noqa: E402
from app.core.ratelimit import SlidingWindowLimiter, login_limiter  # noqa: E402
from app.core.seed.framework import run_seeders  # noqa: E402
from app.core.slices import load_slices  # noqa: E402
from app.jobs.app import job_app  # noqa: E402
from app.jobs.schema import apply_job_schema  # noqa: E402
from app.main import create_app  # noqa: E402

DANA = "dana.dispatcher@example.com"
THEO = "theo.technician@example.com"
VIC = "vic.viewer@example.com"
ADA = "ada.admin@example.com"


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """psycopg async needs the selector loop on Windows; elsewhere use the default loop."""
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


# --- database ------------------------------------------------------------------------------


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    load_slices()
    engine = create_engine()
    await recreate_schema(engine)
    await apply_job_schema()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    set_sessionmaker(factory)
    async with factory() as session:
        await run_seeders(session)
        await session.commit()
    yield engine
    set_sessionmaker(None)
    await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Session inside an outer transaction rolled back after the test."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


# --- infrastructure doubles ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def jobs() -> Iterator[InMemoryConnector]:
    """Every test defers into an in-memory queue (inspect ``jobs.jobs``)."""
    connector = InMemoryConnector()
    with job_app.replace_connector(connector):
        yield connector


@pytest.fixture(autouse=True)
def mailer() -> Iterator[MemoryMailer]:
    memory = MemoryMailer()
    restore = gateways.override(mailer=memory)
    try:
        yield memory
    finally:
        restore()


@pytest.fixture(autouse=True)
def login_throttle() -> Iterator[SlidingWindowLimiter]:
    """Login throttling starts empty in every test (it is per-process state)."""
    login_limiter.reset()
    try:
        yield login_limiter
    finally:
        login_limiter.reset()


@pytest.fixture(autouse=True)
def file_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LocalFileStore]:
    store = LocalFileStore(tmp_path_factory.mktemp("files"))
    set_file_store(store)
    try:
        yield store
    finally:
        set_file_store(None)


# --- application ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app(engine: AsyncEngine) -> FastAPI:
    return create_app()


@pytest.fixture
async def client(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


# --- personas ------------------------------------------------------------------------------


@pytest.fixture
def user(db_session: AsyncSession) -> Callable[[str], Awaitable[User]]:
    async def _get(email: str = DANA) -> User:
        return (await db_session.scalars(select(User).where(User.email == email))).one()

    return _get


@pytest.fixture
def headers(user: Callable[[str], Awaitable[User]]) -> Callable[[str], Awaitable[dict[str, str]]]:
    """``await headers(DANA)`` → ``{"Authorization": "Bearer …"}``."""

    async def _headers(email: str = DANA) -> dict[str, str]:
        token, _ = create_access_token(await user(email))
        return {"Authorization": f"Bearer {token}"}

    return _headers


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - only for anyio-marked tests
    return "asyncio"


TEST_TEMPLATES = Path(__file__).parent / "tests" / "templates"
