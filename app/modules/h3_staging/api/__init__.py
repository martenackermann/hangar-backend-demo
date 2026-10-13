"""H3 routers (mounted under /api/v1 by core)."""

from .fulfilment import router as fulfilment_router
from .staging import router as staging_router

routers = [staging_router, fulfilment_router]

__all__ = ["routers"]
