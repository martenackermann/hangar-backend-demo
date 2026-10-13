"""List endpoints (API.md §1.2–1.3): paging, sorting, comma-separated filters, search.

Response envelope::

    {"data": [...], "meta": {"page": 1, "perPage": 25, "total": 123, "totalPages": 5}}

Typical router code (see CONVENTIONS.md §7)::

    SORT = sort_param({"number": SR.number, "importedAt": SR.imported_at}, default="-importedAt",
                      tiebreaker=SR.id)

    @router.get("", response_model=Page[ThingOut])
    async def list_things(session: SessionDep, paging: PageParamsDep,
                          order: Annotated[list[OrderBy], Depends(SORT)],
                          status: Annotated[list[Status] | None, Depends(csv_enum("status", Status))]):
        ...
        return await paginate(session, stmt, paging, ThingOut)
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Annotated, Any

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy import ColumnElement, Select, String, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.errors import ValidationProblem
from app.core.schemas import ApiModel

DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 200

type Sortable = ColumnElement[Any] | InstrumentedAttribute[Any]
type OrderBy = ColumnElement[Any]


class PageMeta(ApiModel):
    page: int
    per_page: int
    total: int
    total_pages: int


class Page[T](ApiModel):
    data: list[T]
    meta: PageMeta

    @classmethod
    def model_parametrized_name(cls, params: tuple[Any, ...]) -> str:
        # ``Page[ThingOut]`` is published in OpenAPI as ``ThingOutPage``.
        return f"{getattr(params[0], '__name__', 'Item')}Page"


@dataclass(frozen=True, slots=True)
class PageParams:
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    def meta(self, total: int) -> PageMeta:
        return PageMeta(
            page=self.page,
            per_page=self.per_page,
            total=total,
            total_pages=math.ceil(total / self.per_page) if total else 0,
        )


def page_params(
    page: Annotated[int, Query(ge=1, description="1-based page number.")] = 1,
    per_page: Annotated[
        int,
        Query(alias="perPage", ge=1, le=MAX_PER_PAGE, description="Items per page (1–200)."),
    ] = DEFAULT_PER_PAGE,
) -> PageParams:
    return PageParams(page=page, per_page=per_page)


PageParamsDep = Annotated[PageParams, Depends(page_params)]


async def fetch_page(
    session: AsyncSession, stmt: Select[Any], params: PageParams
) -> tuple[list[Any], PageMeta]:
    """Rows of one page + meta, for async builders (``Page[X](data=await build(rows), meta=meta)``)."""
    count_stmt = select(func.count()).select_from(
        stmt.order_by(None).limit(None).offset(None).subquery()
    )
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (await session.scalars(stmt.limit(params.per_page).offset(params.offset))).unique().all()
    return list(rows), params.meta(total)


async def paginate[S: BaseModel](
    session: AsyncSession,
    stmt: Select[Any],
    params: PageParams,
    schema: type[S] | None = None,
    *,
    transform: Callable[[Any], Any] | None = None,
    scalars: bool = True,
) -> Page[S]:
    """Count + fetch one page of ``stmt``.

    Rows are converted with ``transform(row)`` if given, else ``schema.model_validate``.
    Use ``selectinload`` (not ``joinedload``) for collections so the count stays correct.
    """
    count_stmt = select(func.count()).select_from(
        stmt.order_by(None).limit(None).offset(None).subquery()
    )
    total = int((await session.execute(count_stmt)).scalar_one())
    page_stmt = stmt.limit(params.per_page).offset(params.offset)
    if scalars:
        rows: Sequence[Any] = (await session.scalars(page_stmt)).unique().all()
    else:
        rows = [row._mapping for row in (await session.execute(page_stmt)).all()]
    if transform is not None:
        data = [transform(row) for row in rows]
    elif schema is not None:
        data = [schema.model_validate(row) for row in rows]
    else:
        data = list(rows)
    page_model = Page[schema] if schema is not None else Page[Any]
    return page_model(data=data, meta=params.meta(total))


# --- sorting ----------------------------------------------------------------------------------


def parse_sort(raw: str | None, allowed: Sequence[str]) -> list[tuple[str, bool]]:
    """``"a,-b"`` → ``[("a", False), ("b", True)]``; unknown keys → 422 ``errors.sort``."""
    if not raw:
        return []
    result: list[tuple[str, bool]] = []
    seen: set[str] = set()
    problems: list[str] = []
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        descending = token.startswith("-")
        key = token.lstrip("+-")
        if key not in allowed:
            problems.append(f"Unknown sort field '{key}'. Allowed: {', '.join(allowed)}.")
            continue
        if key in seen:
            problems.append(f"Sort field '{key}' is given more than once.")
            continue
        seen.add(key)
        result.append((key, descending))
    if problems:
        raise ValidationProblem({"sort": problems})
    return result


def sort_param(
    allowed: Mapping[str, Sortable],
    *,
    default: str | None = None,
    tiebreaker: Sortable | None = None,
) -> Callable[..., list[OrderBy]]:
    """Dependency turning ``?sort=`` into ``ORDER BY`` clauses (NULLs last, ties by id)."""
    keys = list(allowed)
    description = (
        "Comma-separated sort keys, prefix '-' for descending. "
        f"Allowed: {', '.join(keys)}." + (f" Default: {default}." if default else "")
    )

    def dependency(
        sort: Annotated[str | None, Query(description=description)] = None,
    ) -> list[OrderBy]:
        clauses: list[OrderBy] = []
        for key, descending in parse_sort(sort or default, keys):
            column = allowed[key]
            clauses.append(column.desc().nulls_last() if descending else column.asc().nulls_last())
        if tiebreaker is not None:
            clauses.append(tiebreaker.asc())
        return clauses

    return dependency


# --- comma-separated filters ------------------------------------------------------------------


def split_csv(raw: str | Sequence[str] | None) -> list[str]:
    """Comma-separated values; repeated keys (``?a=x&a=y``) are accepted too (#39)."""
    if raw is None:
        return []
    values = [raw] if isinstance(raw, str) else list(raw)
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def csv_enum[E: StrEnum](
    name: str, enum_cls: type[E], *, description: str | None = None
) -> Callable[..., list[E] | None]:
    """Dependency for ``?<name>=a,b`` enum filters (unknown value → 422 ``errors.<name>``)."""

    def dependency(
        value: Annotated[
            list[str] | None,
            Query(
                alias=name,
                description=description
                or f"Comma-separated: {', '.join(m.value for m in enum_cls)}.",
            ),
        ] = None,
    ) -> list[E] | None:
        parts = split_csv(value)
        if not parts:
            return None
        allowed = {m.value: m for m in enum_cls}
        bad = [p for p in parts if p not in allowed]
        if bad:
            raise ValidationProblem({name: [f"Unknown value '{b}'." for b in bad]})
        return [allowed[p] for p in parts]

    return dependency


def csv_ints(
    name: str, *, allow: Sequence[str] = (), max_items: int = 200
) -> Callable[..., list[int | str] | None]:
    """Dependency for ``?<name>=1,4`` id filters; ``allow`` admits keywords like ``none``/``me``."""

    def dependency(
        value: Annotated[
            list[str] | None, Query(alias=name, description="Comma-separated ids.")
        ] = None,
    ) -> list[int | str] | None:
        parts = split_csv(value)
        if not parts:
            return None
        if len(parts) > max_items:
            raise ValidationProblem({name: [f"At most {max_items} values."]})
        result: list[int | str] = []
        for part in parts:
            if part in allow:
                result.append(part)
            elif part.isdigit():
                result.append(int(part))
            else:
                raise ValidationProblem({name: [f"'{part}' is not a valid id."]})
        return result

    return dependency


def date_range_clause(
    column: Sortable, date_from: date | None, date_to: date | None
) -> list[ColumnElement[bool]]:
    """Inclusive date-range predicates for a ``date`` column."""
    clauses: list[ColumnElement[bool]] = []
    if date_from is not None:
        clauses.append(column >= date_from)
    if date_to is not None:
        clauses.append(column <= date_to)
    return clauses


# --- search -----------------------------------------------------------------------------------


def escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_clause(term: str | None, *columns: Sortable) -> ColumnElement[bool] | None:
    """Case-insensitive "contains" over several columns (``?q=``); ``None`` if no term."""
    if not term or not term.strip():
        return None
    pattern = f"%{escape_like(term.strip())}%"
    return or_(*(sql_cast(column, String).ilike(pattern, escape="\\") for column in columns))
