"""Declarative helpers: naming convention, mixins and ``str_enum`` columns."""

from enum import StrEnum

import pytest
from sqlalchemy import MetaData, String, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.db import NAMING_CONVENTION, Base, IdMixin, TimestampMixin, str_enum


class Color(StrEnum):
    LIGHT_BLUE = "light_blue"
    RED = "red"


class _TestBase(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = Base.type_annotation_map


class Gadget(IdMixin, TimestampMixin, _TestBase):
    __tablename__ = "test_gadgets"

    name: Mapped[str] = mapped_column(String(50), unique=True)
    color: Mapped[Color] = mapped_column(str_enum(Color))


@pytest.fixture
async def session(db_session: AsyncSession) -> AsyncSession:
    await db_session.run_sync(lambda s: _TestBase.metadata.create_all(s.connection()))
    return db_session


def test_constraint_names_follow_convention() -> None:
    table = _TestBase.metadata.tables["test_gadgets"]
    names = {c.name for c in table.constraints}
    assert "pk_test_gadgets" in names
    assert "uq_test_gadgets_name" in names


async def test_mixins_and_enum_values_are_stored(session: AsyncSession) -> None:
    gadget = Gadget(name="g1", color=Color.LIGHT_BLUE)
    session.add(gadget)
    await session.flush()
    await session.refresh(gadget)

    assert gadget.id > 0
    assert gadget.created_at.tzinfo is not None
    raw = (await session.execute(text("SELECT color FROM test_gadgets"))).scalar_one()
    assert raw == "light_blue"  # the value, not the member name


async def test_enum_check_constraint_rejects_unknown_values(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_test_gadgets_color"):
        await session.execute(text("INSERT INTO test_gadgets (name, color) VALUES ('g2', 'green')"))
