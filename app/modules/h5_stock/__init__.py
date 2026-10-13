"""H5 Stock, Inventory & Dashboard slice. Owner of everything listed in README.md."""

from __future__ import annotations

from app.core.slices import SliceRegistry


def register_asset_actions(reg: SliceRegistry) -> None:
    """Asset actions on the core endpoints (SPEC §2.5; ``deactivate`` stays internal, #24)."""
    from .schemas import CorrectBody, MarkFoundBody, MarkMissingBody
    from .services import assets

    reg.register_action(
        resource="assets",
        name="mark_missing",
        payload_model=MarkMissingBody,
        handler=assets.mark_missing,
    )
    reg.register_action(
        resource="assets", name="mark_found", payload_model=MarkFoundBody, handler=assets.mark_found
    )
    reg.register_action(
        resource="assets", name="correct", payload_model=CorrectBody, handler=assets.correct
    )


def register(reg: SliceRegistry) -> None:
    from app.core.demo import table_exporter
    from app.core.events import ImportFinished

    from . import api, notifications, seed, subscribers, tasks
    from .models import H5_TABLES
    from .services.common import RECALC_LOCK, RECALC_TASK

    for router in api.ROUTERS:
        reg.include_router(router)

    register_asset_actions(reg)

    reg.register_template(notifications.CALLOFF_ORDER)
    reg.register_template(notifications.INVENTORY_REPORT)

    # hourly at :20 (UTC — an hourly schedule is time-zone independent), runs serialized
    reg.register_task(tasks.recalculate, name=RECALC_TASK, lock=RECALC_LOCK, periodic="20 * * * *")
    reg.subscribe(ImportFinished, subscribers.on_import_finished)

    reg.register_seeder(50, seed.seed_stock)
    reg.register_seed_exporter(table_exporter(*H5_TABLES))
    reg.register_demo_truncate(list(H5_TABLES))
