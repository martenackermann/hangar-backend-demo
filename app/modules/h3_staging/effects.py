"""In-transaction effects of the H3 SR transitions (SPEC §4.1.1 #17–#25).

One ``Transitioned`` subscriber keeps the effects next to each other and applies them for
every entry point (single action, bulk action, shipment endpoint). ``ship`` is handled
by :func:`app.modules.h3_staging.services.shipping.create_shipment` because it needs the
carrier result.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assets import asset_service
from app.core.enums import AssetAction, AssetStatus, Trigger
from app.core.events import Transitioned
from app.core.models import ServiceRequest, User
from app.core.timeutil import now_utc

H3_ACTIONS = frozenset(
    {
        "claim",
        "release",
        "finish_personalization",
        "skip_preparation",
        "reopen_personalization",
        "close_order",
        "mark_provided",
        "close_without_label",
    }
)


async def _move_new_assets(
    session: AsyncSession,
    sr: ServiceRequest,
    action: AssetAction,
    from_status: AssetStatus,
    actor: User | None,
) -> None:
    for item in sr.new_items:
        asset = item.asset
        if asset is not None and asset.status == from_status:
            await asset_service.move(session, asset, action, actor=actor, trigger=Trigger.SYSTEM)


async def on_transition(session: AsyncSession, event: Transitioned) -> None:
    if event.entity_type != "service_request" or event.action not in H3_ACTIONS:
        return
    sr: ServiceRequest = event.entity
    actor = await session.get(User, event.actor_id) if event.actor_id is not None else None
    now = now_utc()
    match event.action:
        case "claim":
            sr.editor_user_id = event.actor_id
            sr.claimed_at = now
        case "release":
            sr.editor_user_id = None
        case "finish_personalization":
            sr.editor_user_id = None
            sr.personalized_at = now
        case "skip_preparation":
            sr.without_asset_prep = True
            sr.editor_user_id = None
            await _move_new_assets(session, sr, AssetAction.PREPARE, AssetStatus.ASSIGNED, actor)
            sr.order_closed_at = now
        case "reopen_personalization":
            sr.editor_user_id = event.actor_id
            sr.claimed_at = now
        case "close_order":
            await _move_new_assets(session, sr, AssetAction.PREPARE, AssetStatus.ASSIGNED, actor)
            sr.order_closed_at = now
        case "mark_provided" | "close_without_label":
            await _move_new_assets(session, sr, AssetAction.PROVIDE, AssetStatus.PREPARED, actor)
            sr.provided_at = now
        case _:  # pragma: no cover - filtered above
            return
    # keep the ``editor`` relationship in sync with the id for the response builders
    sr.editor = await session.get(User, sr.editor_user_id) if sr.editor_user_id else None
    await session.flush()
