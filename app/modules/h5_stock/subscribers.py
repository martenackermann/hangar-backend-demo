"""Event subscribers: debounced stock recalculation after relevant imports (SPEC §6.7)."""

from __future__ import annotations

from app.core.enums import ImportType
from app.core.events import ImportFinished

from .services.common import defer_debounced_recalculation

STOCK_IMPORTS = frozenset(
    {ImportType.SERVICE_REQUESTS, ImportType.ASSETS_NEW, ImportType.ASSET_STATUS_SYNC}
)


async def on_import_finished(event: ImportFinished) -> None:
    if event.type not in STOCK_IMPORTS:
        return
    await defer_debounced_recalculation()
