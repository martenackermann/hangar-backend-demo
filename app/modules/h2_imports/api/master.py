"""Master data: ``/articles``, ``/regions/{id}``, ``/region-rules``, ``/vip-entries``
(API.md §5.2–§5.4)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import Select, select

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.enums import ArticleCategory, JobKind, RegionRuleMatchKind
from app.core.errors import problem_responses
from app.core.jobs import enqueue
from app.core.models import Article, RegionRule, User
from app.core.pagination import (
    OrderBy,
    Page,
    PageParamsDep,
    csv_enum,
    fetch_page,
    search_clause,
    sort_param,
)
from app.core.schemas import JobAccepted, ListResponse

from ..schemas import (
    ArticleDetail,
    ArticleInput,
    ArticleOut,
    ArticlePatch,
    ArticleSiteSettingOut,
    EmptyBody,
    RegionDetail,
    RegionPatch,
    RegionResolutionResult,
    RegionRuleInput,
    RegionRuleOut,
    RegionRulePatch,
    RegionTestIn,
    SiteSettingPatch,
    VipEntryInput,
    VipEntryOut,
    VipEntryPatch,
)
from ..services import articles as article_service
from ..services import regions as region_service
from ..services import vip as vip_service

type ActiveFilter = Literal["true", "false", "all"]

articles_router = APIRouter(prefix="/articles", tags=["articles"])
regions_router = APIRouter(prefix="/regions", tags=["regions"])
rules_router = APIRouter(prefix="/region-rules", tags=["regions"])
vip_router = APIRouter(prefix="/vip-entries", tags=["vip"])

_READ = Depends(require("master.read"))
_MANAGE = Depends(require("master.manage"))
_ERRORS_WRITE = problem_responses(401, 403, 404, 409, 422)

ARTICLE_SORT = sort_param(
    {
        "number": Article.number,
        "name": Article.name,
        "category": Article.category,
        "internalDescription": Article.internal_description,
    },
    default="number",
    tiebreaker=Article.id,
)


def _active(stmt: Select, column: object, active: ActiveFilter) -> Select:
    if active == "all":
        return stmt
    return stmt.where(column.is_(active == "true"))  # type: ignore[attr-defined]


# --- articles ------------------------------------------------------------------------------


@articles_router.get(
    "",
    response_model=Page[ArticleOut],
    summary="Article catalogue",
    dependencies=[_READ],
    responses=problem_responses(401, 403, 422),
)
async def list_articles(
    session: SessionDep,
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(ARTICLE_SORT)],
    q: Annotated[str | None, Query(max_length=100)] = None,
    category: Annotated[
        list[ArticleCategory] | None, Depends(csv_enum("category", ArticleCategory))
    ] = None,
    active: Annotated[ActiveFilter, Query()] = "true",
) -> Page[ArticleOut]:
    stmt = _active(select(Article), Article.is_active, active).order_by(*order)
    if category:
        stmt = stmt.where(Article.category.in_(category))
    clause = search_clause(
        q,
        Article.number,
        Article.name,
        Article.model_number,
        Article.internal_description,
        Article.manufacturer,
        Article.external_part_number,
    )
    if clause is not None:
        stmt = stmt.where(clause)
    rows, meta = await fetch_page(session, stmt, paging)
    return Page[ArticleOut](data=await article_service.build_articles(session, rows), meta=meta)


@articles_router.get(
    "/{article_id}",
    response_model=ArticleDetail,
    summary="Article with per-hub settings",
    dependencies=[_READ],
    responses=problem_responses(401, 403, 404),
)
async def get_article(article_id: int, session: SessionDep) -> ArticleDetail:
    article = await article_service.get_article(session, article_id)
    return await article_service.build_article_detail(session, article)


@articles_router.post(
    "",
    status_code=201,
    response_model=ArticleDetail,
    summary="Create an article",
    dependencies=[_MANAGE],
    responses=_ERRORS_WRITE,
)
async def create_article(body: ArticleInput, session: SessionDep) -> ArticleDetail:
    article = await article_service.create_article(session, body)
    detail = await article_service.build_article_detail(session, article)
    await commit(session)
    return detail


@articles_router.patch(
    "/{article_id}",
    response_model=ArticleDetail,
    summary="Update an article",
    dependencies=[_MANAGE],
    responses=_ERRORS_WRITE,
)
async def update_article(article_id: int, body: ArticlePatch, session: SessionDep) -> ArticleDetail:
    article = await article_service.get_article(session, article_id)
    await article_service.update_article(session, article, body)
    detail = await article_service.build_article_detail(session, article)
    await commit(session)
    return detail


@articles_router.post(
    "/{article_id}/actions/{action}",
    response_model=ArticleDetail,
    summary="Activate or deactivate an article",
    dependencies=[_MANAGE],
    responses=_ERRORS_WRITE,
)
async def article_action(
    article_id: int,
    action: Literal["activate", "deactivate"],
    session: SessionDep,
    _body: EmptyBody | None = None,
) -> ArticleDetail:
    article = await article_service.get_article(session, article_id)
    await article_service.set_active(session, article, action == "activate")
    detail = await article_service.build_article_detail(session, article)
    await commit(session)
    return detail


@articles_router.patch(
    "/{article_id}/site-settings/{site_id}",
    response_model=ArticleSiteSettingOut,
    summary="Set min/max of an article at a hub",
    dependencies=[_MANAGE],
    responses=_ERRORS_WRITE,
)
async def update_site_setting(
    article_id: int, site_id: int, body: SiteSettingPatch, session: SessionDep
) -> ArticleSiteSettingOut:
    article = await article_service.get_article(session, article_id)
    setting = await article_service.update_site_setting(session, article, site_id, body)
    out = article_service.build_setting(setting)
    await commit(session)
    return out


# --- regions -------------------------------------------------------------------------------


@regions_router.get(
    "/{region_id}",
    response_model=RegionDetail,
    summary="Region with its rules",
    dependencies=[_READ],
    responses=problem_responses(401, 403, 404),
)
async def get_region(region_id: int, session: SessionDep) -> RegionDetail:
    region = await region_service.get_region(session, region_id)
    return await region_service.build_region_detail(session, region)


@regions_router.patch(
    "/{region_id}",
    response_model=RegionDetail,
    summary="Update a region",
    dependencies=[_MANAGE],
    responses=_ERRORS_WRITE,
)
async def update_region(region_id: int, body: RegionPatch, session: SessionDep) -> RegionDetail:
    region = await region_service.get_region(session, region_id)
    await region_service.update_region(session, region, body)
    detail = await region_service.build_region_detail(session, region)
    await commit(session)
    return detail


# --- region rules --------------------------------------------------------------------------


@rules_router.get(
    "",
    response_model=ListResponse[RegionRuleOut],
    summary="Region rules",
    dependencies=[_READ],
    responses=problem_responses(401, 403, 422),
)
async def list_rules(
    session: SessionDep,
    region_id: Annotated[int | None, Query(alias="regionId")] = None,
    match_kind: Annotated[
        list[RegionRuleMatchKind] | None, Depends(csv_enum("matchKind", RegionRuleMatchKind))
    ] = None,
    active: Annotated[ActiveFilter, Query()] = "all",
) -> ListResponse[RegionRuleOut]:
    stmt = _active(select(RegionRule), RegionRule.is_active, active).order_by(
        RegionRule.region_id, RegionRule.match_kind, RegionRule.pattern, RegionRule.id
    )
    if region_id is not None:
        stmt = stmt.where(RegionRule.region_id == region_id)
    if match_kind:
        stmt = stmt.where(RegionRule.match_kind.in_(match_kind))
    rules = (await session.scalars(stmt)).all()
    return ListResponse[RegionRuleOut](data=await region_service.build_rules(session, rules))


@rules_router.post(
    "",
    status_code=201,
    response_model=RegionRuleOut,
    summary="Create a region rule",
    dependencies=[_MANAGE],
    responses=_ERRORS_WRITE,
)
async def create_rule(body: RegionRuleInput, session: SessionDep) -> RegionRuleOut:
    rule = await region_service.create_rule(session, body)
    (out,) = await region_service.build_rules(session, [rule])
    await commit(session)
    return out


@rules_router.post(
    "/test",
    response_model=RegionResolutionResult,
    summary="Test the resolver with an address",
    dependencies=[_READ],
    responses=problem_responses(401, 403, 422),
)
async def test_rules(body: RegionTestIn, session: SessionDep) -> RegionResolutionResult:
    return await region_service.resolve_for_test(session, body)


@rules_router.post(
    "/reapply",
    status_code=202,
    response_model=JobAccepted,
    summary="Re-resolve regions of open requests not yet in staging",
    responses=problem_responses(401, 403, 422),
)
async def reapply_rules(
    session: SessionDep,
    user: Annotated[User, Depends(require("master.manage"))],
    _body: EmptyBody | None = None,
) -> JobAccepted:
    job = await enqueue(
        session,
        "regions.reapply",
        kind=JobKind.REGIONS_REAPPLY,
        created_by=user,
        queueing_lock="regions.reapply",
    )
    job_id = job.id
    await commit(session)
    return JobAccepted(job_id=job_id)


@rules_router.patch(
    "/{rule_id}",
    response_model=RegionRuleOut,
    summary="Update a region rule",
    dependencies=[_MANAGE],
    responses=_ERRORS_WRITE,
)
async def update_rule(rule_id: int, body: RegionRulePatch, session: SessionDep) -> RegionRuleOut:
    rule = await region_service.get_rule(session, rule_id)
    await region_service.update_rule(session, rule, body)
    (out,) = await region_service.build_rules(session, [rule])
    await commit(session)
    return out


@rules_router.delete(
    "/{rule_id}",
    status_code=204,
    response_class=Response,
    summary="Delete a region rule",
    dependencies=[_MANAGE],
    responses=problem_responses(401, 403, 404),
)
async def delete_rule(rule_id: int, session: SessionDep) -> Response:
    rule = await region_service.get_rule(session, rule_id)
    await session.delete(rule)
    await commit(session)
    return Response(status_code=204)


# --- VIP list ------------------------------------------------------------------------------


@vip_router.get(
    "",
    response_model=ListResponse[VipEntryOut],
    summary="VIP list",
    dependencies=[_READ],
    responses=problem_responses(401, 403, 422),
)
async def list_vip_entries(
    session: SessionDep, q: Annotated[str | None, Query(max_length=100)] = None
) -> ListResponse[VipEntryOut]:
    entries = await vip_service.list_entries(session, q)
    return ListResponse[VipEntryOut](data=await vip_service.build_vip_entries(session, entries))


@vip_router.post(
    "",
    status_code=201,
    response_model=VipEntryOut,
    summary="Add a VIP",
    dependencies=[_MANAGE],
    responses=_ERRORS_WRITE,
)
async def create_vip_entry(body: VipEntryInput, session: SessionDep) -> VipEntryOut:
    entry = await vip_service.create_entry(session, body)
    (out,) = await vip_service.build_vip_entries(session, [entry])
    await commit(session)
    return out


@vip_router.patch(
    "/{entry_id}",
    response_model=VipEntryOut,
    summary="Update a VIP entry",
    dependencies=[_MANAGE],
    responses=_ERRORS_WRITE,
)
async def update_vip_entry(entry_id: int, body: VipEntryPatch, session: SessionDep) -> VipEntryOut:
    entry = await vip_service.get_entry(session, entry_id)
    await vip_service.update_entry(session, entry, body)
    (out,) = await vip_service.build_vip_entries(session, [entry])
    await commit(session)
    return out


@vip_router.delete(
    "/{entry_id}",
    status_code=204,
    response_class=Response,
    summary="Remove a VIP",
    dependencies=[_MANAGE],
    responses=problem_responses(401, 403, 404),
)
async def delete_vip_entry(entry_id: int, session: SessionDep) -> Response:
    entry = await vip_service.get_entry(session, entry_id)
    await vip_service.delete_entry(session, entry)
    await commit(session)
    return Response(status_code=204)
