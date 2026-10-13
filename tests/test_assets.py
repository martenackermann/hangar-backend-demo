"""AssetService: movement ledger, location rules, invariants (SPEC §4.3)."""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.assets import AssetError, asset_service
from app.core.enums import (
    AssetAction,
    AssetSource,
    AssetStatus,
    DeactivationReason,
    ServiceRequestStatus,
    Trigger,
)
from app.core.errors import ValidationProblem
from app.core.models import Asset, AssetMovement
from app.testing import factories as f

A = AssetStatus
SR = ServiceRequestStatus


async def _movements(session: AsyncSession, asset: Asset) -> list[AssetMovement]:
    stmt = (
        select(AssetMovement).where(AssetMovement.asset_id == asset.id).order_by(AssetMovement.id)
    )
    return list((await session.scalars(stmt)).all())


async def test_full_lifecycle_writes_the_ledger(db_session: AsyncSession) -> None:
    dana = await f.user(db_session)
    sr = await f.make_sr(db_session, status=SR.PERSONALIZATION_IN_PROGRESS)
    art = await f.article(db_session)
    asset = Asset(
        serial_number="TSLEDGER01",
        article_id=art.id,
        model=art.model_number,
        source=AssetSource.MANUAL,
    )
    goods_in = await f.location(db_session, "RIV-GOODS_IN")
    staging = await f.location(db_session, "RIV-STAGING")
    await asset_service.move(
        db_session,
        asset,
        AssetAction.RECEIVE,
        actor=None,
        trigger=Trigger.IMPORT,
        to_location=goods_in,
    )
    await asset_service.move(
        db_session,
        asset,
        AssetAction.PLACE,
        actor=dana,
        trigger=Trigger.USER,
        to_location=staging,
        installation_place="loc:b4-12",
    )
    assert asset.installation_place == "B4-12"
    await asset_service.move(
        db_session, asset, AssetAction.ASSIGN, actor=dana, trigger=Trigger.USER, service_request=sr
    )
    await asset_service.move(
        db_session, asset, AssetAction.PREPARE, actor=None, trigger=Trigger.SYSTEM
    )
    assert (asset.status, asset.location.code, asset.installation_place) == (
        A.PREPARED,
        "RIV-GOODS_OUT",
        None,
    )
    await asset_service.move(
        db_session, asset, AssetAction.PROVIDE, actor=None, trigger=Trigger.SYSTEM
    )
    await asset_service.move(
        db_session,
        asset,
        AssetAction.DEPLOY,
        actor=None,
        trigger=Trigger.SYSTEM,
        service_request=sr,
    )
    assert (asset.status, asset.location.code, asset.assigned_contact_id) == (
        A.IN_USE,
        "IN_USE",
        sr.contact_id,
    )
    ledger = await _movements(db_session, asset)
    assert [m.action for m in ledger] == [
        "receive",
        "place",
        "assign",
        "prepare",
        "provide",
        "deploy",
    ]
    assert ledger[0].from_status is None
    assert all(a.to_status == b.from_status for a, b in itertools.pairwise(ledger))
    assert ledger[2].service_request_id == sr.id


async def test_assign_requires_place_and_supplying_hub(db_session: AsyncSession) -> None:
    dana = await f.user(db_session)
    sr = await f.make_sr(db_session, status=SR.PERSONALIZATION_IN_PROGRESS)
    no_place = await f.make_asset(db_session, place=None, location_code="RIV-GOODS_IN")
    with pytest.raises(AssetError):
        await asset_service.move(
            db_session,
            no_place,
            AssetAction.ASSIGN,
            actor=dana,
            trigger=Trigger.USER,
            service_request=sr,
        )
    other_hub = await f.make_asset(db_session, location_code="HIL-STAGING")
    with pytest.raises(AssetError, match="supplying hub"):
        await asset_service.move(
            db_session,
            other_hub,
            AssetAction.ASSIGN,
            actor=dana,
            trigger=Trigger.USER,
            service_request=sr,
        )


async def test_not_allowed_from_status(db_session: AsyncSession) -> None:
    asset = await f.make_asset(db_session)
    with pytest.raises(AssetError) as caught:
        await asset_service.move(
            db_session, asset, AssetAction.DEPLOY, actor=None, trigger=Trigger.SYSTEM
        )
    assert caught.value.code == "transition_not_allowed"


async def test_collect_goes_to_campus_return_for_campus_srs(db_session: AsyncSession) -> None:
    sr = await f.make_sr(db_session, status=SR.COMPLETED, campus=True)
    asset = await f.make_asset(db_session, status=A.IN_USE, location_code="IN_USE", place=None)
    await asset_service.move(
        db_session,
        asset,
        AssetAction.COLLECT,
        actor=None,
        trigger=Trigger.SYSTEM,
        service_request=sr,
    )
    assert (asset.status, asset.location.code) == (A.COLLECTED, "CMP-RETURN")
    other = await f.make_sr(db_session, status=SR.COMPLETED, region_code="hillford")
    asset2 = await f.make_asset(db_session, status=A.IN_USE, location_code="IN_USE", place=None)
    await asset_service.move(
        db_session,
        asset2,
        AssetAction.COLLECT,
        actor=None,
        trigger=Trigger.SYSTEM,
        service_request=other,
    )
    assert asset2.location.code == "HIL-RETURN"


async def test_mark_found_and_deactivate_and_correct(db_session: AsyncSession) -> None:
    dana = await f.user(db_session)
    asset = await f.make_asset(db_session)
    await asset_service.move(
        db_session, asset, AssetAction.MARK_MISSING, actor=dana, trigger=Trigger.USER
    )
    returns = await f.location(db_session, "EAS-RETURN")
    await asset_service.move(
        db_session,
        asset,
        AssetAction.MARK_FOUND,
        actor=dana,
        trigger=Trigger.USER,
        to_location=returns,
    )
    assert asset.status == A.COLLECTED
    await asset_service.move(
        db_session,
        asset,
        AssetAction.DEACTIVATE,
        actor=dana,
        trigger=Trigger.USER,
        deactivation_reason=DeactivationReason.BOUGHT_OUT,
        source_ref="deactivation:9",
    )
    assert (asset.status, asset.location.code, asset.service_request_id) == (
        A.DEACTIVATED_BOUGHT_OUT,
        "OFF_FLEET",
        None,
    )
    with pytest.raises(ValidationProblem):
        await asset_service.move(
            db_session,
            asset,
            AssetAction.CORRECT,
            actor=dana,
            trigger=Trigger.USER,
            to_status=A.AVAILABLE,
        )
    with pytest.raises(ValidationProblem) as caught:  # invariant: available must be in stock
        await asset_service.move(
            db_session,
            asset,
            AssetAction.CORRECT,
            actor=dana,
            trigger=Trigger.USER,
            to_status=A.AVAILABLE,
            reason="mistake",
        )
    assert "status" in caught.value.extensions["errors"]
    staging = await f.location(db_session, "EAS-STAGING")
    await asset_service.move(
        db_session,
        asset,
        AssetAction.CORRECT,
        actor=dana,
        trigger=Trigger.USER,
        to_status=A.AVAILABLE,
        to_location=staging,
        installation_place="A1-01",
        reason="mistake",
    )
    ledger = await _movements(db_session, asset)
    assert ledger[-1].action == "correct"
    assert ledger[-1].reason == "mistake"


async def test_status_sync_can_create_assets(db_session: AsyncSession) -> None:
    asset = Asset(serial_number="TSSYNC0001", source=AssetSource.IMPORT_STATUS_SYNC)
    await asset_service.move(
        db_session, asset, AssetAction.SYNC_COLLECTED, actor=None, trigger=Trigger.IMPORT
    )
    assert (asset.status, asset.location.code) == (A.COLLECTED, "RIV-RETURN")
    workflow = await f.make_asset(
        db_session,
        status=A.ASSIGNED,
        sr=await f.make_sr(db_session, status=SR.PERSONALIZATION_IN_PROGRESS),
    )
    with pytest.raises(AssetError):
        await asset_service.move(
            db_session, workflow, AssetAction.SYNC_IN_USE, actor=None, trigger=Trigger.IMPORT
        )
