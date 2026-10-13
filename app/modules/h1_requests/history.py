"""Unified timelines (API.md §4.4 ``HistoryEntry``): transitions + mails + asset movements
(service requests) or transitions + mails + comments (lease returns), newest first."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.core.models import Asset, AssetMovement, LeaseReturnComment, StatusTransition, User
from app.core.read_models import location_ref, user_ref
from app.core.transitions import LEASE_TRANSITIONS, SR_TRANSITIONS

from .models import MailLog
from .schemas import (
    AssetMovementEntry,
    AssetRefLite,
    CommentEntry,
    MailEntry,
    TransitionEntry,
)


def action_label(entity_type: str, action: str) -> str:
    tmap = SR_TRANSITIONS if entity_type == "service_request" else LEASE_TRANSITIONS
    transition = tmap.get(action)
    return transition.label if transition else action.replace("_", " ").capitalize()


async def _users(session: AsyncSession, ids: set[int]) -> dict[int, User]:
    ids.discard(None)
    if not ids:
        return {}
    rows = (await session.scalars(select(User).where(User.id.in_(ids)))).all()
    return {u.id: u for u in rows}


async def _transitions(
    session: AsyncSession, entity_type: str, entity_id: int
) -> list[StatusTransition]:
    return list(
        (
            await session.scalars(
                select(StatusTransition)
                .where(
                    StatusTransition.entity_type == entity_type,
                    StatusTransition.entity_id == entity_id,
                )
                .options(noload(StatusTransition.actor))
            )
        ).all()
    )


async def _mails(session: AsyncSession, entity_type: str, entity_id: int) -> list[MailLog]:
    return list(
        (
            await session.scalars(
                select(MailLog)
                .where(MailLog.entity_type == entity_type, MailLog.entity_id == entity_id)
                .options(noload(MailLog.triggerer))
            )
        ).all()
    )


def _transition_entries(
    rows: list[StatusTransition], entity_type: str, users: dict[int, User]
) -> list[Any]:
    return [
        TransitionEntry(
            id=f"transition:{row.id}",
            occurred_at=row.occurred_at,
            actor=user_ref(users.get(row.actor_user_id)) if row.actor_user_id else None,
            trigger=row.trigger,
            action=row.action,
            action_label=action_label(entity_type, row.action),
            from_status=row.from_status,
            to_status=row.to_status,
            comment=row.comment,
            payload=row.payload,
        )
        for row in rows
    ]


def _mail_entries(rows: list[MailLog], users: dict[int, User]) -> list[Any]:
    return [
        MailEntry(
            id=f"mail:{row.id}",
            occurred_at=row.sent_at or row.queued_at,
            actor=user_ref(users.get(row.triggered_by)) if row.triggered_by else None,
            trigger=row.trigger,
            mail_log_id=row.id,
            template_id=row.template_id,
            subject=row.subject,
            to=list(row.to_addresses),
            status=row.status,
        )
        for row in rows
    ]


def _sorted(entries: list[Any]) -> list[Any]:
    return sorted(entries, key=lambda e: (e.occurred_at, e.id), reverse=True)


async def sr_history(session: AsyncSession, sr_id: int) -> list[Any]:
    transitions = await _transitions(session, "service_request", sr_id)
    mails = await _mails(session, "service_request", sr_id)
    movements = list(
        (
            await session.scalars(
                select(AssetMovement)
                .where(AssetMovement.service_request_id == sr_id)
                .options(
                    noload(AssetMovement.asset),
                    noload(AssetMovement.service_request),
                    noload(AssetMovement.actor),
                )
            )
        ).all()
    )
    asset_ids = {m.asset_id for m in movements}
    assets: dict[int, tuple[int, str, str | None]] = {}
    if asset_ids:
        rows = await session.execute(
            select(Asset.id, Asset.serial_number, Asset.asset_tag).where(Asset.id.in_(asset_ids))
        )
        assets = {row[0]: (row[0], row[1], row[2]) for row in rows.all()}
    users = await _users(
        session,
        {t.actor_user_id for t in transitions if t.actor_user_id}
        | {m.triggered_by for m in mails if m.triggered_by}
        | {m.actor_user_id for m in movements if m.actor_user_id},
    )
    entries: list[Any] = [
        *_transition_entries(transitions, "service_request", users),
        *_mail_entries(mails, users),
    ]
    for movement in movements:
        asset_id, serial, tag = assets.get(movement.asset_id, (movement.asset_id, "?", None))
        to_location = location_ref(movement.to_location)
        assert to_location is not None
        entries.append(
            AssetMovementEntry(
                id=f"asset_movement:{movement.id}",
                occurred_at=movement.occurred_at,
                actor=user_ref(users.get(movement.actor_user_id))
                if movement.actor_user_id
                else None,
                trigger=movement.trigger,
                movement_id=movement.id,
                asset=AssetRefLite(id=asset_id, serial_number=serial, asset_tag=tag),
                action=movement.action,
                from_status=movement.from_status,
                to_status=movement.to_status,
                from_location=location_ref(movement.from_location),
                to_location=to_location,
                reason=movement.reason,
            )
        )
    return _sorted(entries)


async def lease_history(session: AsyncSession, lease_id: int) -> list[Any]:
    transitions = await _transitions(session, "lease_return", lease_id)
    mails = await _mails(session, "lease_return", lease_id)
    comments = list(
        (
            await session.scalars(
                select(LeaseReturnComment)
                .where(LeaseReturnComment.lease_return_id == lease_id)
                .options(noload(LeaseReturnComment.lease_return))
            )
        ).all()
    )
    users = await _users(
        session,
        {t.actor_user_id for t in transitions if t.actor_user_id}
        | {m.triggered_by for m in mails if m.triggered_by},
    )
    entries: list[Any] = [
        *_transition_entries(transitions, "lease_return", users),
        *_mail_entries(mails, users),
    ]
    entries += [
        CommentEntry(
            id=f"comment:{c.id}",
            occurred_at=c.created_at,
            actor=user_ref(c.author),
            trigger="user",  # type: ignore[arg-type]
            body=c.body,
        )
        for c in comments
    ]
    return _sorted(entries)
