"""Shared pytest fixtures for ``tests/`` and ``app/modules/*/tests/``.

The test database is ``TEST_DATABASE_URL`` (default ``hangar_test``). Its schema is
rebuilt once per session (``create_all`` + job schema); every test runs inside a
transaction that is rolled back, so tests may call ``commit()`` freely.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator, Mapping

# --- environment: must be set before anything imports app settings -------------------
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://dev:dev@localhost:5432/hangar_test"
)
os.environ["ENVIRONMENT"] = "test"
os.environ.setdefault("LOG_LEVEL", "WARNING")
_storage_dir = tempfile.mkdtemp(prefix="hangar-test-storage-")
os.environ["STORAGE_DIR"] = _storage_dir
atexit.register(shutil.rmtree, _storage_dir, ignore_errors=True)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from procrastinate.testing import InMemoryConnector  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession  # noqa: E402

from app.core.audit import MemoryAuditSink, set_audit_sink  # noqa: E402
from app.core.db import create_engine, get_session  # noqa: E402
from app.core.dbadmin import recreate_schema  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.core.storage import LocalStorage, get_storage  # noqa: E402
from app.jobs.app import job_app  # noqa: E402
from app.jobs.schema import apply_job_schema  # noqa: E402
from app.main import create_app  # noqa: E402


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]] | None:
    """psycopg async needs the selector loop on Windows."""
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return None


# --- database --------------------------------------------------------------------------


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine()
    await recreate_schema(engine)
    await apply_job_schema()
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session bound to an outer transaction that is rolled back after the test."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


# --- application -----------------------------------------------------------------------


@pytest.fixture(scope="session")
def app() -> FastAPI:
    return create_app()


@pytest.fixture
def storage(tmp_path_factory: pytest.TempPathFactory) -> LocalStorage:
    return LocalStorage(
        tmp_path_factory.mktemp("storage"),
        signing_key=b"test-signing-key",
        base_url="http://test",
    )


@pytest.fixture
async def client(
    app: FastAPI, db_session: AsyncSession, storage: LocalStorage
) -> AsyncIterator[AsyncClient]:
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def jobs() -> Iterator[InMemoryConnector]:
    """Every test defers into an in-memory queue (inspect ``jobs.jobs``)."""
    connector = InMemoryConnector()
    with job_app.replace_connector(connector):
        yield connector


@pytest.fixture
def audit_sink() -> Iterator[MemoryAuditSink]:
    sink = MemoryAuditSink()
    previous = set_audit_sink(sink)
    try:
        yield sink
    finally:
        set_audit_sink(previous)


# --- auth ------------------------------------------------------------------------------


@pytest.fixture
def make_token() -> Callable[..., str]:
    def _make(
        subject: str | int = "user-1", roles: tuple[str, ...] = ("admin",), **kwargs: object
    ) -> str:
        return create_access_token(subject, roles=roles, **kwargs)  # pyright: ignore[reportArgumentType]

    return _make


@pytest.fixture
def auth_headers(make_token: Callable[..., str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token()}"}
