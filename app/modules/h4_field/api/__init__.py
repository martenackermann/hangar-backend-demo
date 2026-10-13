"""H4 routers (mounted under ``/api/v1``)."""

from .calendars import router as calendars_router
from .deployment import router as deployment_router
from .tours import router as tours_router

routers = (calendars_router, tours_router, deployment_router)

__all__ = ["routers"]
