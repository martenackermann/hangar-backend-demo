"""AssetService — the only way to change asset status / location (SPEC §4.3).

Every call validates the action against ``ASSET_TRANSITIONS`` (plus its location rule),
applies the target status/location/installation place/SR link, checks the asset
invariants, appends an ``asset_movements`` row and emits :class:`AssetMoved`.

Creating an asset: build ``Asset(serial_number=..., source=...)`` without status/location,
``session.add`` it and call ``move`` with ``receive`` / ``register_unknown`` / ``sync_*``.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    DEACTIVATED,
    DEACTIVATION_STATUS,
    GLOBAL_LOCATION_TYPES,
    STOCK_TYPES,
    AssetAction,
    AssetStatus,
    DeactivationReason,
    LocationType,
    SiteKind,
    Trigger,
)
from app.core.errors import TransitionNotAllowed, ValidationProblem
from app.core.events import AssetMoved, emit
from app.core.loading import ensure_loaded
from app.core.models import Asset, AssetMovement, ServiceRequest, Site, StockLocation, User
from app.core.rules import normalize_place
from app.core.statemachine import NEW
from app.core.timeutil import now_utc
from app.core.transitions.asset_map import ASSET_TRANSITIONS

A = AssetStatus
L = LocationType


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover
        return "UNSET"


UNSET: Final = _Unset()
_WORKFLOW = frozenset({A.ASSIGNED, A.PREPARED, A.PROVIDED})
_CACHE_KEY = "hangar.locations"


class AssetError(TransitionNotAllowed):
    """409 for an asset action that is not valid from the current state."""


async def stock_location(
    session: AsyncSession, site_id: int | None, type_: LocationType
) -> StockLocation:
    """The location of ``type_`` at ``site_id`` (``None`` for global types); cached per session."""
    cache: dict[tuple[int | None, str], int] = session.info.setdefault(_CACHE_KEY, {})
    key = (None if type_ in GLOBAL_LOCATION_TYPES else site_id, str(type_))
    if key in cache:
        location = await session.get(StockLocation, cache[key])
        if location is not None:
            return location
    stmt = select(StockLocation).where(StockLocation.type == type_)
    stmt = stmt.where(
        StockLocation.site_id.is_(None) if key[0] is None else StockLocation.site_id == key[0]
    )
    location = (await session.scalars(stmt)).first()
    if location is None:
        raise LookupError(f"No stock location of type {type_} at site {site_id}")
    cache[key] = location.id
    return location


async def site_by_code(session: AsyncSession, code: str) -> Site | None:
    return (await session.scalars(select(Site).where(Site.code == code))).first()


async def campus_site(session: AsyncSession) -> Site | None:
    return (await session.scalars(select(Site).where(Site.kind == SiteKind.CAMPUS))).first()


class AssetService:
    async def move(
        self,
        session: AsyncSession,
        asset: Asset,
        action: AssetAction | str,
        *,
        actor: User | None,
        trigger: Trigger,
        to_status: AssetStatus | None = None,
        to_location: StockLocation | None = None,
        installation_place: str | _Unset | None = UNSET,
        service_request: ServiceRequest | _Unset | None = UNSET,
        deactivation_reason: DeactivationReason | None = None,
        reason: str | None = None,
        source_ref: str | None = None,
    ) -> AssetMovement:
        action = AssetAction(action)
        await ensure_loaded(session, asset)
        if isinstance(service_request, ServiceRequest):
            await ensure_loaded(session, service_request)
        transition = ASSET_TRANSITIONS.get(action.value)
        assert transition is not None
        from_status: AssetStatus | None = asset.status
        from_location: StockLocation | None = (
            asset.location if asset.location_id is not None else None
        )
        from_place = asset.installation_place
        from_sr_id = asset.service_request_id
        if not transition.allows_from(from_status) and action != AssetAction.CORRECT:
            raise self._not_allowed(
                asset,
                action,
                f"'{action}' is not allowed while the asset is '{from_status or 'new'}'.",
            )

        sr = asset.service_request if isinstance(service_request, _Unset) else service_request
        new_status: AssetStatus
        new_location: StockLocation
        new_place: str | None = from_place
        new_sr: ServiceRequest | None = asset.service_request

        match action:
            case AssetAction.RECEIVE:
                if from_status is not None and asset.service_request_id is not None:
                    raise self._not_allowed(asset, action, "Asset is linked to a service request.")
                new_location = self._require_location(
                    to_location, action, {L.GOODS_IN, L.STAGING}, hub=True
                )
                new_status = A.AVAILABLE
                new_place = (
                    None
                    if new_location.type != L.STAGING
                    else self._place(installation_place, from_place)
                )
            case AssetAction.PLACE:
                self._require_free_stock(asset, action, {L.GOODS_IN, L.STAGING})
                new_location = self._require_location(to_location, action, {L.STAGING}, hub=True)
                new_place = self._place(installation_place, None)
                if new_place is None:
                    raise ValidationProblem({"place": ["A valid installation place is required."]})
                new_status = A.AVAILABLE
            case AssetAction.ASSIGN:
                self._require_free_stock(asset, action, {L.STAGING})
                if asset.installation_place is None:
                    raise self._not_allowed(asset, action, "Asset has no installation place.")
                if sr is None:
                    raise self._not_allowed(asset, action, "A service request is required.")
                assert from_location is not None
                if from_location.site_id != sr.supplying_site_id:
                    raise self._not_allowed(
                        asset, action, "Asset is not at the supplying hub of the request."
                    )
                new_status, new_location, new_sr = A.ASSIGNED, from_location, sr
            case AssetAction.UNASSIGN:
                assert from_location is not None
                new_status, new_location, new_sr = A.AVAILABLE, from_location, None
            case AssetAction.PREPARE:
                assert from_location is not None
                new_location = await stock_location(session, from_location.site_id, L.GOODS_OUT)
                new_status, new_place = A.PREPARED, None
            case AssetAction.PROVIDE:
                new_location = await stock_location(session, None, L.IN_TRANSIT)
                new_status, new_place = A.PROVIDED, None
            case AssetAction.RETURN_TO_HUB:
                site_id = self._sr_site(asset, sr, action)
                new_location = await stock_location(session, site_id, L.GOODS_OUT)
                new_status, new_place = A.PREPARED, None
            case AssetAction.RELEASE:
                assert from_location is not None
                if from_status == A.ASSIGNED:
                    new_location = from_location
                else:
                    site_id = self._sr_site(asset, sr, action, fallback=from_location.site_id)
                    new_location = await stock_location(session, site_id, L.GOODS_IN)
                    new_place = None
                new_status, new_sr = A.AVAILABLE, None
            case AssetAction.DEPLOY:
                new_location = await stock_location(session, None, L.IN_USE)
                new_status, new_place = A.IN_USE, None
                if sr is not None:
                    asset.assigned_contact = sr.contact
                    new_sr = sr
            case AssetAction.REGISTER_UNKNOWN:
                new_location = await stock_location(session, None, L.IN_USE)
                new_status, new_place, new_sr = A.IN_USE, None, sr
                if sr is not None:
                    asset.assigned_contact = sr.contact
            case AssetAction.COLLECT:
                if sr is None:
                    raise self._not_allowed(asset, action, "A service request is required.")
                site = await campus_site(session) if sr.is_campus else sr.supplying_site
                if site is None:
                    raise self._not_allowed(asset, action, "The request has no supplying hub.")
                new_location = await stock_location(session, site.id, L.RETURN)
                new_status, new_place, new_sr = A.COLLECTED, None, sr
            case AssetAction.SYNC_IN_USE:
                new_location = await stock_location(session, None, L.IN_USE)
                new_status, new_place = A.IN_USE, None
                if not isinstance(service_request, _Unset):
                    new_sr = service_request
            case AssetAction.SYNC_COLLECTED:
                site_id = sr.supplying_site_id if sr is not None else None
                if site_id is None:
                    riverton = await site_by_code(session, "RIV")
                    site_id = riverton.id if riverton else None
                new_location = await stock_location(session, site_id, L.RETURN)
                new_status, new_place = A.COLLECTED, None
                if not isinstance(service_request, _Unset):
                    new_sr = service_request
            case AssetAction.MARK_MISSING:
                assert from_location is not None
                new_status, new_location = A.MISSING, from_location
            case AssetAction.MARK_FOUND | AssetAction.INVENTORY_RELOCATE:
                if to_location is None:
                    raise ValidationProblem({"locationId": ["A target location is required."]})
                new_location = to_location
                if to_location.type in STOCK_TYPES:
                    new_status = A.AVAILABLE
                elif to_location.type == L.RETURN:
                    new_status = A.COLLECTED
                else:
                    raise ValidationProblem({"locationId": ["Choose a stock or returns location."]})
                new_place = (
                    self._place(installation_place, None) if to_location.type == L.STAGING else None
                )
            case AssetAction.DEACTIVATE:
                if to_status is None and deactivation_reason is not None:
                    to_status = DEACTIVATION_STATUS[deactivation_reason]
                if to_status not in DEACTIVATED:
                    raise ValidationProblem({"reason": ["A deactivation reason is required."]})
                new_location = await stock_location(session, None, L.OFF_FLEET)
                assert to_status is not None
                new_status, new_place, new_sr = to_status, None, None
            case AssetAction.CORRECT:
                if not reason or not reason.strip():
                    raise ValidationProblem({"reason": ["A reason is required for corrections."]})
                if from_status is None or from_location is None:
                    raise self._not_allowed(asset, action, "Only existing assets can be corrected.")
                new_status = to_status or from_status
                new_location = to_location or from_location
                if not isinstance(installation_place, _Unset):
                    new_place = normalize_place(installation_place) if installation_place else None
                elif new_location.id != from_location.id:
                    new_place = None
                if new_status in DEACTIVATED or new_status in (A.AVAILABLE, A.MISSING):
                    new_sr = None
            case _:  # pragma: no cover - exhaustive
                raise self._not_allowed(asset, action, "Unsupported asset action.")

        self._check_invariants(asset, action, new_status, new_location, new_place, new_sr)

        now = now_utc()
        if new_status != from_status:
            asset.status_changed_at = now
        asset.status = new_status
        asset.location = new_location
        asset.installation_place = new_place
        asset.service_request = new_sr
        if asset.id is None:
            session.add(asset)
            await session.flush()

        movement = AssetMovement(
            asset=asset,
            action=action,
            from_status=from_status,
            to_status=new_status,
            from_location=from_location,
            to_location=new_location,
            from_installation_place=from_place,
            to_installation_place=new_place,
            service_request_id=(sr.id if sr is not None else None) or from_sr_id,
            actor_user_id=actor.id if actor is not None else None,
            trigger=trigger,
            reason=reason,
            source_ref=source_ref,
            occurred_at=now,
        )
        session.add(movement)
        await session.flush()
        await emit(
            session,
            AssetMoved(
                movement_id=movement.id,
                asset_id=asset.id,
                action=action.value,
                from_status=from_status,
                to_status=new_status,
                service_request_id=movement.service_request_id,
                trigger=trigger,
            ),
        )
        return movement

    # --- helpers --------------------------------------------------------------------------

    @staticmethod
    def _not_allowed(asset: Asset, action: AssetAction, detail: str) -> AssetError:
        return AssetError(
            detail,
            action=action.value,
            current_status=asset.status or NEW,
            allowed_actions=[
                t.action for t in ASSET_TRANSITIONS.from_status(asset.status) if not t.internal
            ],
        )

    def _require_free_stock(
        self, asset: Asset, action: AssetAction, types: set[LocationType]
    ) -> None:
        if asset.status != A.AVAILABLE or asset.service_request_id is not None:
            raise self._not_allowed(asset, action, "Asset is not available.")
        if asset.location is None or asset.location.type not in types:
            raise self._not_allowed(asset, action, "Asset is not in a suitable stock location.")

    @staticmethod
    def _require_location(
        location: StockLocation | None, action: AssetAction, types: set[LocationType], *, hub: bool
    ) -> StockLocation:
        if location is None or location.type not in types:
            allowed = ", ".join(sorted(str(t) for t in types))
            raise ValidationProblem({"locationId": [f"Choose a location of type {allowed}."]})
        if hub and (location.site is None or location.site.kind != SiteKind.HUB):
            raise ValidationProblem({"locationId": ["Choose a location at a hub."]})
        return location

    @staticmethod
    def _place(value: str | _Unset | None, current: str | None) -> str | None:
        if isinstance(value, _Unset):
            return current
        return normalize_place(value) if value else None

    def _sr_site(
        self,
        asset: Asset,
        sr: ServiceRequest | None,
        action: AssetAction,
        fallback: int | None = None,
    ) -> int:
        site_id = sr.supplying_site_id if sr is not None else None
        site_id = site_id or fallback
        if site_id is None:
            raise self._not_allowed(asset, action, "The service request has no supplying hub.")
        return site_id

    @staticmethod
    def _check_invariants(
        asset: Asset,
        action: AssetAction,
        status: AssetStatus,
        location: StockLocation,
        place: str | None,
        sr: ServiceRequest | None,
    ) -> None:
        problems: list[str] = []
        if status in _WORKFLOW and sr is None:
            problems.append(f"Status '{status}' requires a linked service request.")
        if status == A.PROVIDED and location.type != L.IN_TRANSIT:
            problems.append("Provided assets must be in transit.")
        if status == A.IN_USE and location.type != L.IN_USE:
            problems.append("Assets in use must be at the user.")
        if (status in DEACTIVATED) != (location.type == L.OFF_FLEET):
            problems.append("Deactivated assets (and only those) are off fleet.")
        if place is not None and location.type != L.STAGING:
            problems.append("An installation place requires a staging location.")
        if status == A.AVAILABLE and location.type not in STOCK_TYPES:
            problems.append("Available assets must be in a stock location.")
        if status == A.COLLECTED and location.type != L.RETURN:
            problems.append("Collected assets must be at a returns location.")
        if problems:
            if action == AssetAction.CORRECT:
                raise ValidationProblem({"status": problems})
            raise AssetError(
                " ".join(problems), action=action.value, current_status=asset.status or NEW
            )


asset_service = AssetService()


def asset_summary_label(asset: Asset) -> str:
    article = asset.article.name if asset.article else asset.model or "Device"
    return f"{asset.asset_tag or asset.serial_number} · {article}"


__all__: list[Any] = [
    "UNSET",
    "AssetError",
    "AssetService",
    "asset_service",
    "asset_summary_label",
    "campus_site",
    "site_by_code",
    "stock_location",
]
