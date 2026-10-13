"""Application factory. Run with ``uv run hangar serve`` (or
``uvicorn app.main:create_app --factory``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import AsyncConnectionPool

from app import __version__
from app.core.api import actions, auth, meta, misc, reference, saved_views
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import (
    REQUEST_ID_HEADER,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
)
from app.core.openapi import install_openapi, unique_operation_id
from app.core.slices import slice_routers
from app.jobs.app import job_app

log = structlog.get_logger("hangar")

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Small pool used only to *defer* jobs; opened without waiting so the API starts
    # (and reports via /readyz) even if the DB is down.
    pool = AsyncConnectionPool(
        settings.psycopg_conninfo,
        min_size=1,
        max_size=4,
        open=False,
        check=AsyncConnectionPool.check_connection,
    )
    await pool.open(wait=False)
    try:
        async with job_app.open_async(pool=pool):
            log.info("api_started", version=__version__, environment=settings.environment)
            yield
    finally:
        await pool.close()
        await dispose_engine()


def build_api_router() -> APIRouter:
    api = APIRouter(prefix=API_PREFIX)
    for module in (auth, meta, reference, misc, saved_views, actions):
        api.include_router(module.router)
    for router in slice_routers():
        api.include_router(router)
    return api


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    app = FastAPI(
        title="Hangar API",
        version=__version__,
        summary="IT-hardware rollout & logistics console (portfolio demo).",
        lifespan=lifespan,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=f"{API_PREFIX}/docs/oauth2-redirect",
        generate_unique_id_function=unique_operation_id,
    )
    # Innermost first: body limit < CORS (so a 413 still carries CORS headers) < request id.
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_request_bytes,
        upload_bytes=settings.import_max_bytes,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", REQUEST_ID_HEADER],
    )
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(build_api_router())
    install_openapi(app)
    return app
