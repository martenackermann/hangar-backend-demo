"""Type ``assets_new`` — upsert key ``serial_number`` (SPEC §7.6)."""

from __future__ import annotations

from typing import Any

from app.core.assets import asset_service
from app.core.enums import AssetAction, AssetSource, AssetStatus, Trigger
from app.core.models import Asset, StockLocation

from ..context import ApplyContext, JobStats, Outcome, RowEffects, assign_changes
from ..issues import Row, RowError


def asset_fields(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_tag": values.get("asset_tag"),
        "model": values.get("model"),
        "external_asset_id": values.get("external_asset_id"),
        "ci_name": values.get("ci_name"),
        "order_number": values.get("order_number"),
        "mac_address": values.get("mac_address"),
        "checked_in_at": values.get("checked_in_date"),
    }


async def check_tag(ctx: ApplyContext, tag: str | None, serial: str) -> None:
    if not tag:
        return
    other = await ctx.asset_by_tag(tag)
    if other is not None and other.serial_number != serial:
        raise RowError(
            "asset_tag_conflict",
            f"Asset tag {tag} already belongs to serial {other.serial_number}.",
            "asset_tag",
            tag,
        )


async def resolve_article(ctx: ApplyContext, model: str | None, effects: RowEffects) -> Any:
    if not model:
        return None
    article = await ctx.article_for_model(model)
    if article is None:
        effects.warn(
            "unknown_model",
            f"Model '{model}' is not in the article catalogue; the asset has no article.",
            "model",
            model,
        )
    return article


class AssetsNewApplier:
    async def apply(self, ctx: ApplyContext, row: Row, effects: RowEffects) -> Outcome:
        session = ctx.session
        serial = row.key
        assert serial is not None
        target_id = int(ctx.options["targetLocationId"])
        target = await session.get(StockLocation, target_id)
        if target is None:  # pragma: no cover - validated at upload
            raise RowError("invalid_format", "The target location no longer exists.")
        values = row.values
        await check_tag(ctx, values.get("asset_tag"), serial)
        asset = await ctx.asset_by_serial(serial)
        article = await resolve_article(ctx, values.get("model"), effects)
        actor = await ctx.actor()
        if asset is None:
            fields = {k: v for k, v in asset_fields(values).items() if v is not None}
            fields.setdefault("checked_in_at", ctx.now)
            asset = Asset(
                serial_number=serial,
                article=article,
                article_id=article.id if article else None,
                source=AssetSource.IMPORT_NEW,
                **fields,
            )
            await asset_service.move(
                session,
                asset,
                AssetAction.RECEIVE,
                actor=actor,
                trigger=Trigger.IMPORT,
                to_location=target,
                source_ref=ctx.source_ref,
            )
            return "created"
        if asset.status != AssetStatus.AVAILABLE or asset.service_request_id is not None:
            reason = (
                f"linked to a service request ({asset.status})"
                if asset.service_request_id is not None
                else f"'{asset.status}'"
            )
            raise RowError(
                "asset_in_use",
                f"Serial {serial} already exists and is {reason}; it cannot be received again.",
                "serial_number",
                serial,
            )
        changed = assign_changes(asset, asset_fields(values))
        if article is not None and asset.article_id != article.id:
            asset.article, asset.article_id = article, article.id
            changed.add("article_id")
        if asset.location_id != target.id:
            await asset_service.move(
                session,
                asset,
                AssetAction.RECEIVE,
                actor=actor,
                trigger=Trigger.IMPORT,
                to_location=target,
                source_ref=ctx.source_ref,
            )
            changed.add("location_id")
        return "updated" if changed else "unchanged"

    async def post_process(
        self, ctx: ApplyContext, stats: JobStats, *, seen_keys: set[str], complete: bool
    ) -> dict[str, int]:
        return {}
