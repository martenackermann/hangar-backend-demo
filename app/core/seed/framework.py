"""Seed pipeline: registered seeders, JSON exporters and demo truncation (SPEC §11).

``SeedContext`` gives every seeder the same anchor (business date **T**, **D0**, ``wd(n)``)
and its own RNG ``random.Random(42 + order)``. Seeders must be idempotent (skip when
their data exists) — ``hangar seed`` may run on every container start.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Article, Site, StockLocation, User
from app.core.timeutil import anchor_d0, business_datetime, business_today, now_utc, wd

log = structlog.get_logger(__name__)


@dataclass
class SeedContext:
    today: date
    d0: date
    now: datetime
    rng: random.Random
    #: free-form hand-over between seeders (e.g. ids the core seeder created)
    data: dict[str, Any] = field(default_factory=dict)
    _cache: dict[tuple[str, str], Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls, order: int = 0, now: datetime | None = None, shared: dict[str, Any] | None = None
    ) -> SeedContext:
        current = now or now_utc()
        today = business_today(current)
        return cls(
            today,
            anchor_d0(today),
            current,
            random.Random(42 + order),
            shared if shared is not None else {},
        )

    def wd(self, n: int) -> date:
        return wd(self.d0, n)

    def at(self, day: date, hour: int, minute: int = 0) -> datetime:
        return business_datetime(day, hour, minute)

    async def site(self, session: AsyncSession, code: str) -> Site:
        key = ("site", code)
        if key not in self._cache:
            self._cache[key] = (await session.scalars(select(Site).where(Site.code == code))).one()
        return self._cache[key]

    async def location(self, session: AsyncSession, code: str) -> StockLocation:
        key = ("location", code)
        if key not in self._cache:
            self._cache[key] = (
                await session.scalars(select(StockLocation).where(StockLocation.code == code))
            ).one()
        return self._cache[key]

    async def user(self, session: AsyncSession, email: str) -> User:
        key = ("user", email)
        if key not in self._cache:
            self._cache[key] = (
                await session.scalars(select(User).where(User.email == email))
            ).one()
        return self._cache[key]

    async def article(self, session: AsyncSession, number: str) -> Article:
        key = ("article", number)
        if key not in self._cache:
            self._cache[key] = (
                await session.scalars(select(Article).where(Article.number == number))
            ).one()
        return self._cache[key]


type Seeder = Callable[[AsyncSession, SeedContext], Awaitable[None]]
type SeedExporter = Callable[
    [AsyncSession, SeedContext], Awaitable[dict[str, list[dict[str, Any]]]]
]


@dataclass(frozen=True, slots=True)
class SeederEntry:
    order: int
    name: str
    fn: Seeder


_seeders: list[SeederEntry] = []
_exporters: list[tuple[str, SeedExporter]] = []
_truncate: list[str] = []


def register_seeder(order: int, fn: Seeder, name: str | None = None) -> None:
    entry = SeederEntry(order, name or getattr(fn, "__qualname__", "seeder"), fn)
    if all(e.fn is not fn for e in _seeders):
        _seeders.append(entry)


def register_seed_exporter(fn: SeedExporter, name: str | None = None) -> None:
    if all(f is not fn for _, f in _exporters):
        _exporters.append((name or getattr(fn, "__qualname__", "exporter"), fn))


def register_demo_truncate(tables: list[str] | tuple[str, ...]) -> None:
    for table in tables:
        if table not in _truncate:
            _truncate.append(table)


def seeders() -> list[SeederEntry]:
    return sorted(_seeders, key=lambda e: (e.order, e.name))


def exporters() -> list[tuple[str, SeedExporter]]:
    return list(_exporters)


def truncate_tables() -> list[str]:
    return list(_truncate)


async def run_seeders(session: AsyncSession, now: datetime | None = None) -> list[str]:
    """Run all seeders in order (one transaction, caller commits). Returns their names."""
    shared: dict[str, Any] = {}
    ran: list[str] = []
    for entry in seeders():
        ctx = SeedContext.create(entry.order, now, shared)
        log.info("seeding", seeder=entry.name, order=entry.order)
        await entry.fn(session, ctx)
        await session.flush()
        ran.append(entry.name)
    return ran


async def truncate_demo_data(session: AsyncSession) -> None:
    """Empty all registered data tables. ``RESTART IDENTITY`` so personas keep their ids.

    ``background_jobs`` is emptied too (it references ``users``); ``demo.reset`` restores
    its own job row afterwards (SPEC_RESOLUTIONS #48).
    """
    tables = truncate_tables()
    if tables:
        await session.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
