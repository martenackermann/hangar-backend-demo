"""``/contacts`` (API.md §4.6) and ``/pipeline/summary`` (§4.7)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import exists, select

from app.core.auth import require
from app.core.db import SessionDep, commit
from app.core.errors import NotFoundError, problem_responses
from app.core.models import Contact, User, VipEntry
from app.core.pagination import OrderBy, Page, PageParamsDep, fetch_page, sort_param
from app.core.read_models import (
    ContactDetail,
    ContactListItem,
    build_contact_detail,
    build_contact_summaries,
)

from ..queries import PipelineFiltersDep, contact_match
from ..schemas import ContactPatch, PipelineSummary
from ..services import patch_contact, pipeline_summary

router = APIRouter(prefix="/contacts", tags=["contacts"])
pipeline_router = APIRouter(prefix="/pipeline", tags=["pipeline"])

SORT = sort_param(
    {"lastName": Contact.last_name, "userId": Contact.user_id},
    default="lastName",
    tiebreaker=Contact.id,
)
Reader = Annotated[User, Depends(require("sr.read"))]


@router.get(
    "",
    response_model=Page[ContactListItem],
    summary="List contacts",
    responses=problem_responses(401, 403, 422),
)
async def list_contacts(
    session: SessionDep,
    _user: Reader,
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
    q: Annotated[str | None, Query(description="Name, email or user id.")] = None,
    vip: Annotated[
        Literal["exclude", "include", "only"],
        Query(description="include (default) | exclude | only"),
    ] = "include",
) -> Page[ContactListItem]:
    stmt = select(Contact)
    if q and q.strip():
        stmt = stmt.where(contact_match(q))
    is_vip = exists().where(VipEntry.user_id == Contact.user_id)
    if vip == "only":
        stmt = stmt.where(is_vip)
    elif vip == "exclude":
        stmt = stmt.where(~is_vip)
    rows, meta = await fetch_page(session, stmt.order_by(*order), paging)
    return Page[ContactListItem](data=await build_contact_summaries(session, rows), meta=meta)


async def _get(session: SessionDep, contact_id: int) -> Contact:
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise NotFoundError(f"Contact {contact_id} not found.")
    return contact


@router.get(
    "/{contact_id}",
    response_model=ContactDetail,
    summary="Contact detail",
    responses=problem_responses(401, 403, 404),
)
async def get_contact(contact_id: int, session: SessionDep, _user: Reader) -> ContactDetail:
    return await build_contact_detail(session, await _get(session, contact_id))


@router.patch(
    "/{contact_id}",
    response_model=ContactDetail,
    summary="Edit contact",
    responses=problem_responses(401, 403, 404, 422),
)
async def patch_contact_endpoint(
    contact_id: int,
    body: ContactPatch,
    session: SessionDep,
    _user: Annotated[User, Depends(require("contacts.update"))],
) -> ContactDetail:
    contact = await _get(session, contact_id)
    await patch_contact(session, contact, body)
    await commit(session)
    return await build_contact_detail(session, contact)


@pipeline_router.get(
    "/summary",
    response_model=PipelineSummary,
    summary="Pipeline board summary",
    responses=problem_responses(401, 403, 422),
)
async def get_pipeline_summary(
    session: SessionDep, _user: Reader, filters: PipelineFiltersDep
) -> PipelineSummary:
    return await pipeline_summary(session, filters)
