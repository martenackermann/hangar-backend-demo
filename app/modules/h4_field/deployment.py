"""Technician deployment (SPEC §6.11): stop context, scans, verification, unknown devices,
completion effects of SR #26–#29."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assets import asset_service
from app.core.auth import can
from app.core.enums import (
    LR_TERMINAL,
    OLD_SLOTS,
    AssetAction,
    AssetSource,
    AssetStatus,
    ItemSlot,
    ScanVerdict,
    ServiceRequestStatus,
    TourStopOutcome,
    Trigger,
    label,
)
from app.core.errors import (
    ApiError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationProblem,
)
from app.core.loading import ensure_loaded
from app.core.models import (
    Article,
    Asset,
    DeviationCode,
    LeaseReturn,
    ServiceRequest,
    ServiceRequestItem,
    User,
)
from app.core.read_models import (
    ServiceRequestItemOut,
    build_contact_detail,
    build_item,
    build_sr_detail,
)
from app.core.read_models.master import build_deviation_code
from app.core.schemas import DeliveryAddress, ScanProgress, ScanResult, UndoLink
from app.core.timeutil import now_utc
from app.core.transitions import transition_service

from .common import (
    NOT_OWN_TOUR,
    ON_SITE_STATUSES,
    can_execute_tour,
    counted_items,
    is_new_slot,
    latest_stop,
    normalize_code,
    pending_stop,
)
from .models import Tour, TourStop
from .schemas import DeploymentProgress, DeploymentViewOut, WorkInstructions
from .tours import build_stop, build_tour_summary, ensure_running, load_tour, set_outcome

SR = ServiceRequestStatus


@dataclass(frozen=True, slots=True)
class StopContext:
    stop: TourStop | None
    tour: Tour | None


# --- authorization / context ------------------------------------------------------------------


def require_executor(user: User) -> None:
    if not (can(user, "tours.execute_own") or can(user, "tours.execute_any")):
        raise ForbiddenError(
            "You do not have permission to perform this action.",
            requiredPermissions=["tours.execute_own", "tours.execute_any"],
        )


async def stop_context(
    session: AsyncSession, sr: ServiceRequest, user: User, *, pending_only: bool = True
) -> StopContext:
    """The SR's pending (or latest) stop; ``403 not_own_tour`` unless the user may execute it."""
    require_executor(user)
    stop = await (pending_stop(session, sr.id) if pending_only else latest_stop(session, sr.id))
    tour = stop.tour if stop is not None else None
    if tour is None:
        if not can(user, "tours.execute_any"):
            raise ForbiddenError("This request is not on one of your tours.", code=NOT_OWN_TOUR)
    elif not can_execute_tour(user, tour):
        raise ForbiddenError("This stop belongs to another technician's tour.", code=NOT_OWN_TOUR)
    return StopContext(stop, tour)


async def active_context(session: AsyncSession, sr: ServiceRequest, user: User) -> StopContext:
    """Context for scans/verification: SR handed over, tour running (auto-start, #32)."""
    if sr.status not in ON_SITE_STATUSES:
        raise ConflictError(
            f"Hardware not handed over yet (status: {label(SR(sr.status), SR)}). Contact dispatch."
        )
    context = await stop_context(session, sr, user)
    if context.tour is not None:
        from .guards import tour_startable

        failure = await tour_startable(session, sr, user, None)
        if failure is not None:
            raise ConflictError(failure.message)
        await ensure_running(session, context.tour, user)
    return context


# --- progress ---------------------------------------------------------------------------------


def progress_of(sr: ServiceRequest) -> DeploymentProgress:
    new = [i for i in sr.items if is_new_slot(i.slot) and i.asset_id is not None]
    old = [i for i in sr.items if not is_new_slot(i.slot)]
    return DeploymentProgress(
        new_verified=sum(1 for i in new if i.verified_at is not None),
        new_total=len(new),
        old_verified=sum(1 for i in old if i.verified_at is not None),
        old_total=len(old),
    )


def scan_progress(sr: ServiceRequest) -> ScanProgress:
    items = counted_items(sr)
    return ScanProgress(done=sum(1 for i in items if i.verified_at is not None), total=len(items))


def suggested_slots(sr: ServiceRequest) -> list[ItemSlot]:
    """Unverified old slots + the first free old slot (register an extra device)."""
    existing = {i.slot: i for i in sr.items if not is_new_slot(i.slot)}
    slots = [ItemSlot(s) for s, i in existing.items() if i.verified_at is None]
    free = [s for s in OLD_SLOTS if s not in existing]
    if free:
        slots.append(free[0])
    return slots


def _verify(item: ServiceRequestItem, asset: Asset | None, scanned: str, user: User) -> None:
    item.verified_at = now_utc()
    item.verified_by = user.id
    item.verified_by_user = user
    item.verified_serial = asset.serial_number if asset is not None else scanned


def _undo(sr: ServiceRequest, item: ServiceRequestItem) -> UndoLink:
    return UndoLink(
        method="DELETE",
        href=f"/service-requests/{sr.id}/items/{item.slot}/verification",
        body=None,
    )


# --- scan (§6.11 table; SPEC_RESOLUTIONS #26: "already verified" first) ------------------------


async def find_asset(session: AsyncSession, value: str) -> Asset | None:
    return (
        await session.scalars(
            select(Asset).where(or_(Asset.serial_number == value, Asset.asset_tag == value))
        )
    ).first()


def _matches(asset: Asset | None, value: str) -> bool:
    return asset is not None and value in (asset.serial_number, asset.asset_tag)


async def scan(
    session: AsyncSession, sr: ServiceRequest, raw: str, user: User
) -> ScanResult[ServiceRequestItemOut]:
    value = normalize_code(raw)
    if not value:
        raise ValidationProblem({"serial": ["Scan a serial number or asset tag."]})
    await active_context(session, sr, user)
    await ensure_loaded(session, sr)

    def result(
        verdict: ScanVerdict,
        code: str,
        message: str,
        item: ServiceRequestItem | None = None,
        *,
        applied: bool = False,
        can_register: bool = False,
    ) -> ScanResult[ServiceRequestItemOut]:
        return ScanResult[ServiceRequestItemOut](
            verdict=verdict,
            code=code,
            message=message,
            scanned_value=value,
            applied=applied,
            entity=build_item(item) if item is not None else None,
            undo=_undo(sr, item) if applied and item is not None else None,
            progress=scan_progress(sr),
            can_register=can_register,
            suggested_slots=suggested_slots(sr) if can_register else [],
        )

    # 1. already verified on this SR
    for item in sr.items:
        if item.verified_at is not None and (
            item.verified_serial == value or _matches(item.asset, value)
        ):
            return result(
                ScanVerdict.OK,
                "already_verified",
                f"{label(ItemSlot(item.slot), ItemSlot)} already verified.",
                item,
            )
    asset = await find_asset(session, value)
    # 2. new item's asset
    for item in sr.new_items:
        if item.asset is not None and _matches(item.asset, value):
            _verify(item, item.asset, value, user)
            await session.flush()
            article = item.asset.article.name if item.asset.article else item.requested_type
            return result(
                ScanVerdict.OK,
                "new_verified",
                f"{label(ItemSlot(item.slot), ItemSlot)} verified: {article} "
                f"({item.asset.asset_tag or item.asset.serial_number})",
                item,
                applied=True,
            )
    # 3. old item's asset, or an asset carrying an old item's expected tag
    for item in sr.old_items:
        if item.verified_at is not None:
            continue
        linked = item.asset is not None and _matches(item.asset, value)
        expected = (
            asset is not None
            and item.expected_asset_tag is not None
            and asset.asset_tag == normalize_code(item.expected_asset_tag)
        )
        if linked or expected:
            target = item.asset if linked else asset
            if item.asset_id is None and target is not None:
                item.asset = target
                item.asset_id = target.id
            _verify(item, target, value, user)
            await session.flush()
            tag = target.asset_tag if target is not None else value
            return result(
                ScanVerdict.OK,
                "old_verified",
                f"{label(ItemSlot(item.slot), ItemSlot)} verified for collection: {tag}",
                item,
                applied=True,
            )
    if asset is not None:
        # 4. new item of another SR
        other = (
            await session.execute(
                select(ServiceRequest.number)
                .join(
                    ServiceRequestItem, ServiceRequestItem.service_request_id == ServiceRequest.id
                )
                .where(
                    ServiceRequestItem.asset_id == asset.id,
                    ServiceRequestItem.slot.like("new_%"),
                    ServiceRequest.id != sr.id,
                )
                .order_by(ServiceRequest.id.desc())
            )
        ).scalar()
        if other is not None:
            return result(ScanVerdict.ERROR, "belongs_to_other_sr", f"Device belongs to {other}.")
        # 5. not linked to this SR
        return result(
            ScanVerdict.ERROR,
            "not_on_this_request",
            "Device is not part of this request. Contact dispatch.",
        )
    # 6. unknown
    return result(
        ScanVerdict.WARNING,
        "unknown_device",
        "Unknown device. Register it as an old device if you collect it.",
        can_register=True,
    )


# --- verification undo / register unknown -----------------------------------------------------


def _item_or_404(sr: ServiceRequest, slot: ItemSlot) -> ServiceRequestItem:
    item = sr.item(slot)
    if item is None:
        raise NotFoundError(f"{sr.number} has no item {slot}.")
    return item


async def undo_verification(
    session: AsyncSession, sr: ServiceRequest, slot: ItemSlot, user: User
) -> ServiceRequestItem:
    await active_context(session, sr, user)
    item = _item_or_404(sr, slot)
    if item.verified_at is None:
        raise ApiError(
            "This device is not verified.",
            status=409,
            code="undo_not_possible",
            title="Undo not possible",
        )
    asset = item.asset
    if (
        asset is not None
        and asset.source == AssetSource.TECHNICIAN_SCAN
        and asset.service_request_id == sr.id
        and not is_new_slot(item.slot)
    ):
        raise ApiError(
            "A registered device stays on the request; its verification cannot be undone.",
            status=409,
            code="undo_not_possible",
            title="Undo not possible",
        )
    item.verified_at = None
    item.verified_by = None
    item.verified_by_user = None
    item.verified_serial = None
    await session.flush()
    return item


async def register_unknown(
    session: AsyncSession,
    sr: ServiceRequest,
    slot: ItemSlot,
    *,
    serial: str,
    asset_tag: str | None,
    model: str | None,
    user: User,
) -> ServiceRequestItem:
    if is_new_slot(slot):
        raise ValidationProblem({"slot": ["Only old-device slots can take a registered device."]})
    value = normalize_code(serial)
    tag = normalize_code(asset_tag) or None
    if not value:
        raise ValidationProblem({"serial": ["A serial number is required."]})
    await active_context(session, sr, user)
    await ensure_loaded(session, sr)
    duplicate = (
        await session.scalars(
            select(Asset).where(
                or_(Asset.serial_number == value, *([Asset.asset_tag == tag] if tag else []))
            )
        )
    ).first()
    if duplicate is not None:
        raise ApiError(
            f"A device with this {'serial' if duplicate.serial_number == value else 'asset tag'} "
            "already exists. Scan it instead.",
            status=409,
            code="duplicate",
            title="Duplicate",
        )
    item = sr.item(slot)
    if item is not None and item.verified_at is not None:
        raise ConflictError(f"{label(slot, ItemSlot)} is already verified.")
    article = None
    clean_model = (model or "").strip() or None
    if clean_model:
        article = (
            await session.scalars(
                select(Article).where(func.lower(Article.model_number) == clean_model.lower())
            )
        ).first()
    asset = Asset(
        serial_number=value,
        asset_tag=tag,
        model=clean_model,
        article=article,
        article_id=article.id if article else None,
        source=AssetSource.TECHNICIAN_SCAN,
    )
    session.add(asset)
    await asset_service.move(
        session,
        asset,
        AssetAction.REGISTER_UNKNOWN,
        actor=user,
        trigger=Trigger.USER,
        service_request=sr,
        source_ref="scan:deployment",
    )
    if item is None:
        item = ServiceRequestItem(slot=slot, expected_asset_tag=tag, asset=asset, asset_id=asset.id)
        sr.items.append(item)
    else:
        item.asset = asset
        item.asset_id = asset.id
    _verify(item, asset, value, user)
    await session.flush()
    return item


# --- view -------------------------------------------------------------------------------------


async def deployment_view(
    session: AsyncSession, tour_id: int, stop_id: int, user: User
) -> DeploymentViewOut:
    require_executor(user)
    tour = await load_tour(session, tour_id)
    stop = next((s for s in tour.stops if s.id == stop_id), None)
    if stop is None:
        raise NotFoundError(f"Stop {stop_id} is not part of tour {tour_id}.")
    if not can_execute_tour(user, tour):
        raise ForbiddenError("This stop belongs to another technician's tour.", code=NOT_OWN_TOUR)
    sr = stop.service_request
    await ensure_loaded(session, sr)
    codes = (
        await session.scalars(
            select(DeviationCode)
            .where(DeviationCode.is_active.is_(True))
            .order_by(DeviationCode.sort_order, DeviationCode.id)
        )
    ).all()
    software: list[str] = list(
        dict.fromkeys([*(sr.requested_software or []), *(sr.added_software or [])])
    )
    return DeploymentViewOut(
        tour=build_tour_summary(tour, user),
        stop=build_stop(stop),
        service_request=await build_sr_detail(session, sr, user),
        contact=await build_contact_detail(session, sr.contact),
        work_instructions=WorkInstructions(
            procurement_type=sr.procurement_type,
            scope_of_delivery=sr.scope_of_delivery,
            client_component=sr.client_component,
            os_reinstall=sr.os_reinstall,
            without_asset_prep=sr.without_asset_prep,
            software=software,
            note=sr.note,
            onsite_contact_phone=sr.onsite_contact_phone,
            delivery_address=DeliveryAddress(
                street=sr.delivery_street,
                zip=sr.delivery_zip,
                city=sr.delivery_city,
                country=sr.delivery_country,
                room=sr.delivery_room,
                building=sr.delivery_building,
            ),
            is_campus=sr.is_campus,
        ),
        deviation_codes=[build_deviation_code(c) for c in codes],
        progress=progress_of(sr),
    )


# --- completion effects (SR #26–#29) -----------------------------------------------------------


async def close_collected_leases(
    session: AsyncSession, sr: ServiceRequest, collected: list[Asset], actor: User | None
) -> None:
    """SPEC §6.4 / SPEC_RESOLUTIONS #30: close non-closed lease returns of collected tags."""
    tags = [a.asset_tag for a in collected if a.asset_tag]
    if not tags:
        return
    leases = (
        await session.scalars(
            select(LeaseReturn).where(
                LeaseReturn.asset_tag.in_(tags), LeaseReturn.status.not_in(LR_TERMINAL)
            )
        )
    ).all()
    for lease in leases:
        lease.closed_at = now_utc()
        lease.close_reason = "device_collected"
        await transition_service.apply(
            session,
            lease,
            "close",
            actor=actor,
            trigger=Trigger.SYSTEM,
            comment=f"Device collected on site ({sr.number}).",
        )


async def mark_stop(session: AsyncSession, sr_id: int, outcome: TourStopOutcome) -> None:
    stop = await pending_stop(session, sr_id)
    if stop is not None:
        await set_outcome(session, stop, outcome)


async def completion_effects(
    session: AsyncSession, sr: ServiceRequest, actor: User, *, verified_only: bool
) -> None:
    """New assets ``deploy`` (verified only for #26; all for #29) / ``release``; verified old
    assets ``collect`` + lease close (#26 only); stop ``completed``; ``completed_at``."""
    await ensure_loaded(session, sr)
    for item in sr.new_items:
        asset = item.asset
        if asset is None or asset.status != AssetStatus.PROVIDED:
            continue
        if not verified_only or item.verified_at is not None:
            await asset_service.move(
                session,
                asset,
                AssetAction.DEPLOY,
                actor=actor,
                trigger=Trigger.SYSTEM,
                service_request=sr,
            )
        else:
            await asset_service.move(
                session,
                asset,
                AssetAction.RELEASE,
                actor=actor,
                trigger=Trigger.SYSTEM,
                service_request=sr,
            )
            item.asset = None
            item.asset_id = None
    if verified_only:
        collected: list[Asset] = []
        for item in sr.old_items:
            asset = item.asset
            if item.verified_at is None or asset is None:
                continue
            if asset.status in (AssetStatus.IN_USE, AssetStatus.MISSING):
                await asset_service.move(
                    session,
                    asset,
                    AssetAction.COLLECT,
                    actor=actor,
                    trigger=Trigger.SYSTEM,
                    service_request=sr,
                )
            collected.append(asset)
        await close_collected_leases(session, sr, collected, actor)
    await mark_stop(session, sr.id, TourStopOutcome.COMPLETED)
    sr.completed_at = now_utc()
    await session.flush()


async def cancel_on_site_effects(session: AsyncSession, sr: ServiceRequest, actor: User) -> None:
    await ensure_loaded(session, sr)
    for item in sr.new_items:
        asset = item.asset
        if asset is not None and asset.status in (
            AssetStatus.PROVIDED,
            AssetStatus.PREPARED,
            AssetStatus.ASSIGNED,
        ):
            await asset_service.move(
                session,
                asset,
                AssetAction.RELEASE,
                actor=actor,
                trigger=Trigger.SYSTEM,
                service_request=sr,
            )
            item.asset = None
            item.asset_id = None
    await mark_stop(session, sr.id, TourStopOutcome.CANCELLED)
    sr.cancelled_at = now_utc()
    await session.flush()
