"""Database plumbing: async engine/session, declarative ``Base`` and shared mixins.

Conventions (see CONVENTIONS.md):
- table and column names are snake_case; constraint names follow ``NAMING_CONVENTION``
  so Alembic autogenerate produces stable, reviewable names;
- all datetimes are timezone-aware (``Mapped[datetime]`` maps to ``timestamptz``);
- write paths call :func:`commit` (not ``session.commit()``) so post-commit hooks such
  as job deferral run only after the data is durable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

import orjson
from fastapi import Depends
from sqlalchemy import BigInteger, DateTime, Identity, MetaData, event, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every model. Import models via ``app.modules.<name>.models``."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


class IdMixin:
    """Bigint identity primary key ``id``."""

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained by the database / ORM."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def str_enum[E: StrEnum](enum_cls: type[E], *, length: int = 40) -> SAEnum:
    """Column type for a ``StrEnum``: VARCHAR + CHECK constraint storing the enum *values*.

    No native PostgreSQL enum types, so adding a state is a one-line migration::

        status: Mapped[SrState] = mapped_column(str_enum(SrState), index=True)
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        length=length,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
        name=_snake(enum_cls.__name__),
    )


def _snake(name: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() and i else c.lower() for i, c in enumerate(name))


# --- engine / sessions ---------------------------------------------------------------

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
    }
    options.update(kwargs)
    if options.get("poolclass") is NullPool:
        options.pop("pool_size")
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


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request. Nothing is committed implicitly."""
    async with get_sessionmaker()() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


# --- commit hooks --------------------------------------------------------------------

_ON_COMMIT_KEY = "hangar.on_commit"
OnCommitCallback = Callable[[], Awaitable[None]]


def on_commit(session: AsyncSession, callback: OnCommitCallback) -> None:
    """Run ``callback`` after the next successful :func:`commit` of ``session``.

    Typical use: defer a procrastinate job or send a mail only once the data it refers
    to is committed. Callbacks are discarded on rollback.
    """
    session.info.setdefault(_ON_COMMIT_KEY, []).append(callback)


async def commit(session: AsyncSession) -> None:
    """Commit, then run callbacks registered with :func:`on_commit` (in order)."""
    await session.commit()
    callbacks: list[OnCommitCallback] = session.info.pop(_ON_COMMIT_KEY, [])
    for callback in callbacks:
        await callback()


@event.listens_for(Session, "after_soft_rollback")
def _discard_commit_hooks(session: Session, previous_transaction: Any) -> None:
    if previous_transaction.parent is None:  # outermost transaction only
        session.info.pop(_ON_COMMIT_KEY, None)
