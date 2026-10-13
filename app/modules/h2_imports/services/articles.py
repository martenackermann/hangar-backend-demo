"""Article catalogue + per-hub min/max (API.md §5.2)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    DEACTIVATED,
    DEMAND_STATUSES,
    NEW_SLOTS,
    ServiceRequestStatus,
    SiteKind,
)
from app.core.errors import ConflictError, NotFoundError, ValidationProblem
from app.core.models import (
    Article,
    ArticleSiteSetting,
    Asset,
    ServiceRequest,
    ServiceRequestItem,
    Site,
)
from app.core.read_models.refs import site_ref
from app.core.stock_math import ensure_article_site_settings

from ..schemas import (
    ArticleDetail,
    ArticleInput,
    ArticleOut,
    ArticlePatch,
    ArticleSiteSettingOut,
    SiteSettingPatch,
)

_UNIQUE_MESSAGES = {
    "number": "Another article already uses this number.",
    "modelNumber": "Another article already uses this model number.",
    "internalDescription": "Another active article already uses this device type.",
}


async def article_counts(
    session: AsyncSession, ids: Sequence[int]
) -> tuple[dict[int, int], dict[int, int]]:
    """(asset count without deactivated assets, open demand) per article id."""
    if not ids:
        return {}, {}
    assets = await session.execute(
        select(Asset.article_id, func.count())
        .where(Asset.article_id.in_(ids), Asset.status.not_in(DEACTIVATED))
        .group_by(Asset.article_id)
    )
    demand = await session.execute(
        select(ServiceRequestItem.article_id, func.count())
        .join(ServiceRequest, ServiceRequest.id == ServiceRequestItem.service_request_id)
        .where(
            ServiceRequestItem.article_id.in_(ids),
            ServiceRequestItem.slot.in_(NEW_SLOTS),
            or_(
                ServiceRequest.status.in_(DEMAND_STATUSES),
                (ServiceRequest.status == ServiceRequestStatus.RESCHEDULE_NEEDED)
                & ServiceRequest.resume_status.is_distinct_from(ServiceRequestStatus.SHIPPED),
            ),
        )
        .group_by(ServiceRequestItem.article_id)
    )
    return (
        {a: int(n) for a, n in assets.all() if a is not None},
        {a: int(n) for a, n in demand.all() if a is not None},
    )


def _out(article: Article, assets: int, demand: int) -> dict[str, object]:
    return {
        "id": article.id,
        "number": article.number,
        "category": article.category,
        "name": article.name,
        "model_number": article.model_number,
        "manufacturer": article.manufacturer,
        "external_part_number": article.external_part_number,
        "internal_description": article.internal_description,
        "weight_kg": float(article.weight_kg),
        "is_active": article.is_active,
        "asset_count": assets,
        "open_demand": demand,
    }


async def build_articles(session: AsyncSession, articles: Sequence[Article]) -> list[ArticleOut]:
    assets, demand = await article_counts(session, [a.id for a in articles])
    return [
        ArticleOut.model_validate(_out(a, assets.get(a.id, 0), demand.get(a.id, 0)))
        for a in articles
    ]


def build_setting(setting: ArticleSiteSetting) -> ArticleSiteSettingOut:
    site = site_ref(setting.site)
    assert site is not None
    return ArticleSiteSettingOut(
        site=site,
        buffer_qty=setting.buffer_qty,
        min_qty=setting.min_qty,
        max_qty=setting.max_qty,
        stock_qty=setting.stock_qty,
        demand_qty=setting.demand_qty,
        calculated_at=setting.calculated_at,
        last_order_date=setting.last_order_date,
        last_ordered_qty=setting.last_ordered_qty,
    )


async def build_article_detail(session: AsyncSession, article: Article) -> ArticleDetail:
    await session.flush()
    await session.refresh(article)
    assets, demand = await article_counts(session, [article.id])
    settings = (
        await session.scalars(
            select(ArticleSiteSetting)
            .join(Site, Site.id == ArticleSiteSetting.site_id)
            .where(ArticleSiteSetting.article_id == article.id)
            .order_by(Site.sort_order, Site.id)
        )
    ).all()
    return ArticleDetail.model_validate(
        {
            **_out(article, assets.get(article.id, 0), demand.get(article.id, 0)),
            "site_settings": [build_setting(s) for s in settings],
            "created_at": article.created_at,
            "updated_at": article.updated_at,
        }
    )


async def get_article(session: AsyncSession, article_id: int) -> Article:
    article = await session.get(Article, article_id)
    if article is None:
        raise NotFoundError(f"Article {article_id} not found.")
    return article


async def _check_unique(
    session: AsyncSession,
    *,
    number: str,
    model_number: str,
    internal_description: str,
    active: bool,
    exclude_id: int | None = None,
) -> None:
    errors: dict[str, list[str]] = {}

    async def exists(*clauses: object) -> bool:
        stmt = select(Article.id).where(*clauses)  # pyright: ignore[reportArgumentType]
        if exclude_id is not None:
            stmt = stmt.where(Article.id != exclude_id)
        return (await session.scalar(stmt.limit(1))) is not None

    if await exists(func.lower(Article.number) == number.lower()):
        errors["number"] = [_UNIQUE_MESSAGES["number"]]
    if await exists(func.lower(Article.model_number) == model_number.lower()):
        errors["modelNumber"] = [_UNIQUE_MESSAGES["modelNumber"]]
    if active and await exists(
        func.lower(Article.internal_description) == internal_description.lower(),
        Article.is_active,
    ):
        errors["internalDescription"] = [_UNIQUE_MESSAGES["internalDescription"]]
    if errors:
        raise ConflictError("The article conflicts with an existing article.", errors=errors)


async def create_article(session: AsyncSession, body: ArticleInput) -> Article:
    await _check_unique(
        session,
        number=body.number,
        model_number=body.model_number,
        internal_description=body.internal_description,
        active=True,
    )
    article = Article(**body.model_dump(by_alias=False), is_active=True)
    session.add(article)
    await session.flush()
    await ensure_article_site_settings(session, article.id)
    return article


async def update_article(session: AsyncSession, article: Article, body: ArticlePatch) -> Article:
    values = body.model_dump(exclude_unset=True, by_alias=False)
    required = {"number", "category", "name", "model_number", "manufacturer"}
    required |= {"internal_description", "weight_kg"}
    nulls = [k for k, v in values.items() if v is None and k in required]
    if nulls:
        raise ValidationProblem({_camel(k): ["This field cannot be empty."] for k in nulls})
    await _check_unique(
        session,
        number=values.get("number", article.number),
        model_number=values.get("model_number", article.model_number),
        internal_description=values.get("internal_description", article.internal_description),
        active=article.is_active,
        exclude_id=article.id,
    )
    for name, value in values.items():
        setattr(article, name, value)
    await session.flush()
    return article


async def set_active(session: AsyncSession, article: Article, active: bool) -> Article:
    if active and not article.is_active:
        clash = await session.scalar(
            select(Article.number).where(
                func.lower(Article.internal_description) == article.internal_description.lower(),
                Article.is_active,
                Article.id != article.id,
            )
        )
        if clash is not None:
            raise ConflictError(
                f"Article {clash} is active with the same device type "
                f"'{article.internal_description}'. Deactivate it first.",
                errors={"internalDescription": [_UNIQUE_MESSAGES["internalDescription"]]},
            )
    article.is_active = active
    await session.flush()
    return article


async def update_site_setting(
    session: AsyncSession, article: Article, site_id: int, body: SiteSettingPatch
) -> ArticleSiteSetting:
    site = await session.get(Site, site_id)
    if site is None or site.kind != SiteKind.HUB:
        raise NotFoundError(f"Hub {site_id} not found.")
    if body.min_qty is not None and body.max_qty is not None and body.max_qty < body.min_qty:
        raise ValidationProblem({"maxQty": ["Max must be greater than or equal to min."]})
    setting = (
        await session.scalars(
            select(ArticleSiteSetting).where(
                ArticleSiteSetting.article_id == article.id, ArticleSiteSetting.site_id == site.id
            )
        )
    ).first()
    if setting is None:
        setting = ArticleSiteSetting(article_id=article.id, site_id=site.id)
        session.add(setting)
    setting.min_qty = body.min_qty
    setting.max_qty = body.max_qty
    await session.flush()
    await session.refresh(setting)
    return setting


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)
