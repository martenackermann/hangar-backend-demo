"""H5 HTTP routers (API.md §8)."""

from fastapi import APIRouter

from . import assets, calloff, dashboard, deactivations, inventories, stock

ROUTERS: tuple[APIRouter, ...] = (
    stock.router,
    calloff.router,
    assets.router,
    deactivations.router,
    inventories.router,
    dashboard.router,
)
