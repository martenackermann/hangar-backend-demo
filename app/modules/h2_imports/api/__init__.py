"""H2 routers."""

from fastapi import APIRouter

from .imports import router as imports_router
from .master import articles_router, regions_router, rules_router, vip_router
from .users import router as users_router

ROUTERS: tuple[APIRouter, ...] = (
    imports_router,
    articles_router,
    regions_router,
    rules_router,
    vip_router,
    users_router,
)
