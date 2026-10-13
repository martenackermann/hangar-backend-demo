"""H1 routers (mounted under ``/api/v1`` by core)."""

from .comms import router as comms_router
from .contacts import pipeline_router
from .contacts import router as contacts_router
from .lease_returns import router as lease_returns_router
from .service_requests import router as service_requests_router

ROUTERS = (
    service_requests_router,
    lease_returns_router,
    contacts_router,
    pipeline_router,
    comms_router,
)

__all__ = ["ROUTERS"]
