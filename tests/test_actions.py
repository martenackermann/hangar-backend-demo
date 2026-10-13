"""ActionRegistry + generic action endpoints: availableActions/blockedActions, authorizers,
422/404/409 handling, bulk, asset actions (SPEC §2.4, API.md §1.5/§3.8, SPEC_RESOLUTIONS #2/#24/#34)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actions import ActionBody, actions
from app.core.assets import asset_service, stock_location
from app.core.enums import AssetAction, AssetStatus, LocationType, ServiceRequestStatus, Trigger
from app.core.models import Asset, User
from app.core.statemachine import authorizers, guards
from app.core.transitions import transition_service
from app.testing import factories as f

SR = ServiceRequestStatus
OWNER = "test"


class PausePayload(ActionBody):
    reason: str = Field(min_length=1, max_length=500)


class CorrectPayload(ActionBody):
    status: AssetStatus | None = None
    reason: str = Field(min_length=1)


async def _pause(session: AsyncSession, sr: Any, actor: User, payload: ActionBody) -> None:
    assert isinstance(payload, PausePayload)
    sr.communication_paused = True
    sr.communication_pause_reason = payload.reason
    await transition_service.apply(
        session, sr, "pause_communication", actor=actor, trigger=Trigger.USER, payload=payload
    )


async def _mark_missing(
    session: AsyncSession, asset: Any, actor: User, payload: ActionBody
) -> None:
    await asset_service.move(
        session,
        asset,
        AssetAction.MARK_MISSING,
        actor=actor,
        trigger=Trigger.USER,
        reason=payload.comment,
    )


async def _correct(session: AsyncSession, asset: Any, actor: User, payload: ActionBody) -> None:
    assert isinstance(payload, CorrectPayload)
    await asset_service.move(
        session,
        asset,
        AssetAction.CORRECT,
        actor=actor,
        trigger=Trigger.USER,
        to_status=payload.status,
        reason=payload.reason,
    )


@pytest.fixture(autouse=True)
def registered() -> Iterator[None]:
    for name in ("send_invitation", "send_next_mail", "cancel", "claim", "mark_provided", "ship"):
        actions.register(resource="service_requests", name=name, owner=OWNER)
    actions.register(
        resource="service_requests",
        name="pause_communication",
        payload_model=PausePayload,
        handler=_pause,
        owner=OWNER,
    )
    actions.register(resource="lease_returns", name="send_info", owner=OWNER)
    actions.register(resource="assets", name="mark_missing", handler=_mark_missing, owner=OWNER)
    actions.register(
        resource="assets",
        name="correct",
        payload_model=CorrectPayload,
        handler=_correct,
        owner=OWNER,
    )
    yield
    actions.unregister_owner(OWNER)
    guards.remove_owner(OWNER)
    authorizers.remove_owner(OWNER)


async def test_available_and_blocked_actions(db_session: AsyncSession) -> None:
    dana = await f.user(db_session)
    ok = await f.make_sr(db_session, status=SR.IMPORTED)
    available, blocked = await actions.available(db_session, ok, dana)
    assert "send_invitation" in available
    assert "send_next_mail" in available
    assert "cancel" in available
    assert blocked == []

    no_mail = await f.make_sr(db_session, status=SR.IMPORTED, email=None)
    available, blocked = await actions.available(db_session, no_mail, dana)
    assert "send_invitation" not in available
    assert {(b.action, b.guard) for b in blocked} >= {
        ("send_invitation", "contact_email"),
        ("send_next_mail", "contact_email"),
    }

    viewer = await f.user(db_session, "vic.viewer@example.com")
    assert await actions.available_names(db_session, ok, viewer) == []


async def test_ship_is_listed_but_not_dispatchable(
    db_session: AsyncSession,
    client: AsyncClient,
    headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    dana = await f.user(db_session)
    sr = await f.make_sr(
        db_session, status=SR.READY_FOR_PICKING, region_code="westbay", assign_new=True, packed=True
    )
    assert "ship" in await actions.available_names(db_session, sr, dana)
    response = await client.post(
        f"/api/v1/service-requests/{sr.id}/actions/ship",
        json={},
        headers=await headers("dana.dispatcher@example.com"),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_action"


async def test_action_endpoint_success_and_detail(
    db_session: AsyncSession,
    client: AsyncClient,
    headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    sr = await f.make_sr(db_session, status=SR.IMPORTED)
    response = await client.post(
        f"/api/v1/service-requests/{sr.id}/actions/send_invitation",
        json={"comment": "first mail"},
        headers=await headers("dana.dispatcher@example.com"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "invited"
    assert body["stage"] == "invited"
    assert body["version"] == 2
    assert body["communicationDate"] is not None
    assert "sendReminder1" not in body  # camelCase payload keys only
    assert "availableActions" in body
    assert "blockedActions" in body


async def test_action_errors(
    db_session: AsyncSession,
    client: AsyncClient,
    headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    sr = await f.make_sr(db_session, status=SR.INVITED)
    auth = await headers("dana.dispatcher@example.com")
    url = f"/api/v1/service-requests/{sr.id}/actions"
    unknown = await client.post(f"{url}/fly", json={}, headers=auth)
    assert (unknown.status_code, unknown.json()["code"]) == (404, "unknown_action")
    not_allowed = await client.post(f"{url}/send_invitation", json={}, headers=auth)
    assert not_allowed.status_code == 409
    assert not_allowed.json()["code"] == "transition_not_allowed"
    assert "send_reminder_1" in not_allowed.json()["allowedActions"]
    missing_comment = await client.post(f"{url}/cancel", json={}, headers=auth)
    assert missing_comment.status_code == 422
    assert "comment" in missing_comment.json()["errors"]
    extra = await client.post(f"{url}/cancel", json={"comment": "x", "bogus": 1}, headers=auth)
    assert extra.status_code == 422
    assert extra.json()["errors"]["bogus"] == ["Unknown field."]
    stale = await client.post(f"{url}/cancel", json={"comment": "dup", "version": 99}, headers=auth)
    assert stale.status_code == 409
    assert stale.json()["code"] == "version_conflict"
    assert stale.json()["currentVersion"] == sr.version
    viewer = await client.post(
        f"{url}/cancel", json={"comment": "x"}, headers=await headers("vic.viewer@example.com")
    )
    assert viewer.status_code == 403
    missing = await client.post(
        "/api/v1/service-requests/999999/actions/cancel", json={"comment": "x"}, headers=auth
    )
    assert missing.status_code == 404


async def test_handler_action_with_payload(
    db_session: AsyncSession,
    client: AsyncClient,
    headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    sr = await f.make_sr(db_session, status=SR.INVITED)
    auth = await headers("dana.dispatcher@example.com")
    bad = await client.post(
        f"/api/v1/service-requests/{sr.id}/actions/pause_communication",
        json={"reason": ""},
        headers=auth,
    )
    assert bad.status_code == 422
    ok = await client.post(
        f"/api/v1/service-requests/{sr.id}/actions/pause_communication",
        json={"reason": "Leave"},
        headers=auth,
    )
    assert ok.status_code == 200
    assert ok.json()["communicationPaused"] is True
    assert ok.json()["status"] == "invited"


async def test_bulk_reports_per_id_outcomes(
    db_session: AsyncSession,
    client: AsyncClient,
    headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    good = await f.make_sr(db_session, status=SR.IMPORTED)
    no_mail = await f.make_sr(db_session, status=SR.IMPORTED, email=None)
    done = await f.make_sr(db_session, status=SR.INVITED)
    good_id, no_mail_id, done_id = good.id, no_mail.id, done.id  # rollbacks expire objects
    response = await client.post(
        "/api/v1/service-requests/bulk/send_invitation",
        json={"ids": [good_id, no_mail_id, done_id, 999999]},
        headers=await headers("dana.dispatcher@example.com"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "send_invitation"
    assert body["succeeded"] == [good_id]
    codes = {item["id"]: item["code"] for item in body["failed"]}
    assert codes == {
        no_mail_id: "guard_failed",
        done_id: "transition_not_allowed",
        999999: "not_found",
    }
    not_bulk = await client.post(
        "/api/v1/service-requests/bulk/claim",
        json={"ids": [good_id]},
        headers=await headers("dana.dispatcher@example.com"),
    )
    assert not_bulk.status_code == 404


async def test_authorizer_hides_action_and_returns_403(
    db_session: AsyncSession,
    client: AsyncClient,
    headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    from app.core.slices import SliceRegistry

    async def only_ada(session: AsyncSession, entity: Any, actor: User) -> bool:
        return actor.email == "ada.admin@example.com"

    SliceRegistry(owner=OWNER).register_authorizer(
        "service_requests", "cancel", only_ada, code="not_own_tour"
    )
    dana = await f.user(db_session)
    sr = await f.make_sr(db_session, status=SR.IMPORTED)
    available, blocked = await actions.available(db_session, sr, dana)
    assert "cancel" not in available
    assert all(b.action != "cancel" for b in blocked)
    response = await client.post(
        f"/api/v1/service-requests/{sr.id}/actions/cancel",
        json={"comment": "no"},
        headers=await headers("dana.dispatcher@example.com"),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "not_own_tour"


async def test_registered_slice_guard_blocks(db_session: AsyncSession) -> None:
    from app.core.slices import SliceRegistry
    from app.core.statemachine import GuardFailure

    async def never(
        session: AsyncSession, entity: Any, actor: User | None, payload: Any
    ) -> GuardFailure | None:
        return GuardFailure("calendar_enabled", "Calendar disabled.")

    SliceRegistry(owner=OWNER).register_guard(
        "service_request", "claim", never, code="calendar_enabled"
    )
    dana = await f.user(db_session)
    sr = await f.make_sr(db_session, status=SR.APPOINTMENT_BOOKED, appointment=None)
    _, blocked = await actions.available(db_session, sr, dana)
    guard_codes = {b.guard for b in blocked if b.action == "claim"}
    assert guard_codes == {"has_appointment"}  # core guard runs first
    sr.appointment_start_at = sr.imported_at
    _, blocked = await actions.available(db_session, sr, dana)
    assert {b.guard for b in blocked if b.action == "claim"} == {"calendar_enabled"}


async def test_asset_actions_and_deactivate_not_exposed(
    db_session: AsyncSession,
    client: AsyncClient,
    headers: Callable[[str], Awaitable[dict[str, str]]],
) -> None:
    asset = await f.make_asset(db_session)
    auth = await headers("dana.dispatcher@example.com")
    response = await client.post(
        f"/api/v1/assets/{asset.id}/actions/mark_missing",
        json={"comment": "not on shelf"},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "missing"
    assert response.json()["recentMovements"][0]["action"] == "mark_missing"
    deactivate = await client.post(
        f"/api/v1/assets/{asset.id}/actions/deactivate", json={}, headers=auth
    )
    assert deactivate.status_code == 404
    correct = await client.post(
        f"/api/v1/assets/{asset.id}/actions/correct", json={"status": "available"}, headers=auth
    )
    assert correct.status_code == 422
    technician = await client.post(
        f"/api/v1/assets/{asset.id}/actions/mark_missing",
        json={},
        headers=await headers("theo.technician@example.com"),
    )
    assert technician.status_code == 403
    loaded = await db_session.get(Asset, asset.id)
    assert loaded is not None
    assert (
        await stock_location(db_session, loaded.location.site_id, LocationType.STAGING)
    ).id == loaded.location_id
