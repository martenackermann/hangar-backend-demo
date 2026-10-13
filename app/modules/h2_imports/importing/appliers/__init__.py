"""One applier per import type (SPEC §7.3–§7.7)."""

from __future__ import annotations

from typing import Protocol

from app.core.enums import ImportType

from ..context import ApplyContext, JobStats, Outcome, RowEffects
from ..issues import Row


class Applier(Protocol):
    async def apply(self, ctx: ApplyContext, row: Row, effects: RowEffects) -> Outcome:
        """Apply one valid row inside its SAVEPOINT; raise ``RowError`` to reject it."""
        ...

    async def post_process(
        self, ctx: ApplyContext, stats: JobStats, *, seen_keys: set[str], complete: bool
    ) -> dict[str, int]:
        """Type-specific post-processing (own transaction); returns ``post_process`` counts.

        ``complete`` is ``False`` when the import was cancelled before the last chunk.
        """
        ...


def applier_for(import_type: ImportType) -> Applier:
    from .assets_new import AssetsNewApplier
    from .contacts import ContactsApplier
    from .leases import LeaseReturnsApplier
    from .service_requests import ServiceRequestsApplier
    from .status_sync import StatusSyncApplier

    appliers: dict[ImportType, type[Applier]] = {
        ImportType.SERVICE_REQUESTS: ServiceRequestsApplier,
        ImportType.LEASE_RETURNS: LeaseReturnsApplier,
        ImportType.CONTACTS: ContactsApplier,
        ImportType.ASSETS_NEW: AssetsNewApplier,
        ImportType.ASSET_STATUS_SYNC: StatusSyncApplier,
    }
    return appliers[import_type]()
