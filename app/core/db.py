"""Database plumbing: async engine/session, declarative ``Base``, mixins, unit of work.

Conventions (CONVENTIONS.md §5):
- constraint names follow :data:`NAMING_CONVENTION` (stable Alembic diffs);
- enums are VARCHAR(32) + CHECK via :func:`str_enum` (no native PG enums, SPEC §0);
- ``Mapped[datetime]`` is ``timestamptz``; the connection time zone is UTC;
- one request/job = one transaction: services never commit; the caller runs
  :func:`commit`, which then runs after-commit callbacks (job deferral, mail, events).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

import orjson
import structlog
from fastapi import Depends
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Identity,
    Integer,
    MetaData,
    Numeric,
    event,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, declared_attr, mapped_column
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

log = structlog.get_logger(__name__)

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    # enum CHECKs are unnamed -> named after their column; explicit CHECKs carry full names
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Python-side default for timestamps the code reads right after insert."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for every model (core: ``app.core.models``; slices: ``models.py``).

    ``eager_defaults`` fetches server-generated values (``created_at``/``updated_at``) with
    RETURNING, so they never trigger lazy loads in async code.
    """

    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        datetime: DateTime(timezone=True),
        date: Date,
        Decimal: Numeric(10, 2),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


def str_enum[E: StrEnum](enum_cls: type[E], *, length: int = 32) -> SAEnum:
    """VARCHAR(``length``) + CHECK storing the enum *values* (SPEC §0 "Enums in DB")."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        length=length,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
        name=None,
    )


class IdMixin:
    """``integer GENERATED ALWAYS AS IDENTITY`` primary key."""

    id: Mapped[int] = mapped_column(
        Integer, Identity(always=True), primary_key=True, sort_order=-100
    )


class BigIdMixin:
    """``bigint`` identity primary key for high-volume ledgers."""

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True, sort_order=-100
    )


class TimestampMixin:
    """``created_at`` / ``updated_at`` (every table, SPEC §0)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, sort_order=1000
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        sort_order=1001,
    )


class VersionMixin:
    """Optimistic lock column (SPEC §0), incremented by the ORM on every UPDATE of the row."""

    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1", sort_order=999
    )

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:  # noqa: N805 - declarative directive
        return {"version_id_col": cls.version, "eager_defaults": True}


# --- engine / sessions -------------------------------------------------------------------

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _json_serializer(value: Any) -> str:
    return orjson.dumps(value, default=str).decode()


def create_engine(url: str | None = None, **kwargs: Any) -> AsyncEngine:
    settings = get_settings()
    options: dict[str, Any] = {
        "echo": settings.database_echo,
        "pool_pre_ping": True,
        "pool_size": settings.database_pool_size,
        "json_serializer": _json_serializer,
        "json_deserializer": orjson.loads,
        # timestamps come back as UTC -> serialized with "Z"
        "connect_args": {"options": "-c timezone=UTC"},
    }
    options.update(kwargs)
    if options.get("poolclass") is NullPool:
        options.pop("pool_size", None)
    return create_async_engine(url or settings.database_url, **options)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


def set_sessionmaker(factory: async_sessionmaker[AsyncSession] | None) -> None:
    """Override the session factory (tests bind it to the test engine)."""
    global _sessionmaker
    _sessionmaker = factory


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def open_session() -> AsyncSession:
    """A fresh session for jobs and after-commit work (``async with open_session() as s``)."""
    return get_sessionmaker()()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request. Nothing is committed implicitly."""
    async with open_session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


# --- unit of work ------------------------------------------------------------------------

_AFTER_COMMIT_KEY = "hangar.after_commit"
AfterCommit = Callable[[], Awaitable[None]]


def after_commit(session: AsyncSession, callback: AfterCommit) -> None:
    """Run ``callback`` after the next successful :func:`commit` of ``session``.

    Used for job deferral, ``notify()`` delivery and after-commit event handlers.
    Callbacks are discarded on rollback. Failures are logged, never raised.
    """
    session.info.setdefault(_AFTER_COMMIT_KEY, []).append(callback)


#: Backwards-compatible alias used by 2a code and docs.
on_commit = after_commit


async def commit(session: AsyncSession) -> None:
    """Commit the unit of work, then run after-commit callbacks in order."""
    await session.commit()
    callbacks: list[AfterCommit] = session.info.pop(_AFTER_COMMIT_KEY, [])
    for callback in callbacks:
        try:
            await callback()
        except Exception:
            log.exception("after_commit_failed", callback=getattr(callback, "__qualname__", "?"))


@event.listens_for(Session, "after_soft_rollback")
def _discard_after_commit(session: Session, previous_transaction: Any) -> None:
    if previous_transaction.parent is None:
        session.info.pop(_AFTER_COMMIT_KEY, None)
