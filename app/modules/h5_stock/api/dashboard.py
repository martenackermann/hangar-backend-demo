"""Dashboard KPIs (API.md §8.6)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.auth import require
from app.core.db import SessionDep
from app.core.errors import problem_responses
from app.core.models import User

from ..schemas import DashboardKpis
from ..services.dashboard import build_kpis

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get(
    "/kpis",
    response_model=DashboardKpis,
    summary="Dashboard KPIs, series and breakdowns",
    responses=problem_responses(401, 403, 422),
)
async def get_kpis(
    session: SessionDep,
    _user: Annotated[User, Depends(require("dashboard.read"))],
    site_id: Annotated[int | None, Query(alias="siteId")] = None,
    region_id: Annotated[int | None, Query(alias="regionId")] = None,
) -> DashboardKpis:
    return await build_kpis(session, site_id, region_id)
