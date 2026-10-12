"""List endpoints: ``?page=&perPage=`` pagination, ``?sort=a,-b`` sorting, free-text search.

Response envelope::

    {"data": [...], "meta": {"page": 1, "perPage": 25, "total": 123, "totalPages": 5}}

Typical router code::

    SORT = sort_param({"createdAt": Thing.created_at, "name": Thing.name}, default="-createdAt")

    @router.get("", response_model=Page[ThingOut])
    async def list_things(session: SessionDep, paging: PageParamsDep,
                          order: Annotated[list[OrderBy], Depends(SORT)]):
        stmt = select(Thing).order_by(*order)
        return await paginate(session, stmt, paging, ThingOut)
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy import ColumnElement, Select, String, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.errors import DomainValidationError
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
        Query(alias="perPage", ge=1, le=MAX_PER_PAGE, description="Items per page (max 200)."),
    ] = DEFAULT_PER_PAGE,
) -> PageParams:
    return PageParams(page=page, per_page=per_page)


PageParamsDep = Annotated[PageParams, Depends(page_params)]


async def paginate[S: BaseModel](
    session: AsyncSession,
    stmt: Select[Any],
    params: PageParams,
    schema: type[S],
    *,
    scalars: bool = True,
) -> Page[S]:
    """Count + fetch one page of ``stmt`` and convert rows with ``schema.model_validate``.

    ``scalars=False`` validates ``row._mapping`` (for multi-column selects).
    Use ``selectinload`` (not ``joinedload``) for collections so the count stays correct.
    """
    count_stmt = select(func.count()).select_from(
        stmt.order_by(None).limit(None).offset(None).subquery()
    )
    total = int((await session.execute(count_stmt)).scalar_one())
    page_stmt = stmt.limit(params.per_page).offset(params.offset)
    if scalars:
        rows: Sequence[Any] = (await session.scalars(page_stmt)).all()
    else:
        rows = [row._mapping for row in (await session.execute(page_stmt)).all()]
    page_model = Page[schema]
    return page_model(data=[schema.model_validate(row) for row in rows], meta=params.meta(total))


# --- sorting -------------------------------------------------------------------------


def parse_sort(raw: str | None, allowed: Sequence[str]) -> list[tuple[str, bool]]:
    """Parse ``"a,-b"`` into ``[("a", False), ("b", True)]`` (second item = descending).

    Raises :class:`DomainValidationError` (422, field ``sort``) for unknown or repeated keys.
    """
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
        raise DomainValidationError({"sort": problems})
    return result


def sort_param(
    allowed: Mapping[str, Sortable],
    *,
    default: str | None = None,
    tiebreaker: Sortable | None = None,
) -> Callable[..., list[OrderBy]]:
    """Build a dependency turning ``?sort=`` into ``ORDER BY`` clauses.

    ``allowed`` maps public camelCase keys to columns. ``tiebreaker`` (usually the primary
    key) is appended so pagination is deterministic. NULLs always sort last.
    """
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


# --- filtering -----------------------------------------------------------------------


def escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_clause(term: str | None, *columns: Sortable) -> ColumnElement[bool] | None:
    """Case-insensitive "contains" over several columns (``?q=``); ``None`` if no term."""
    if not term or not term.strip():
        return None
    pattern = f"%{escape_like(term.strip())}%"
    return or_(*(sql_cast(column, String).ilike(pattern, escape="\\") for column in columns))
