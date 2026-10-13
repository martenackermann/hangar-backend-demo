"""Core seed story (SPEC §11, #41–47): counts, consistency, idempotency, JSON export, reset."""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.demo import build_seed_json, reset_and_seed
from app.core.enums import AttentionFlag, LeaseReturnStatus, ServiceRequestStatus
from app.core.models import (
    Asset,
    Contact,
    LeaseReturn,
    ServiceRequest,
    StatusTransition,
    User,
    VipEntry,
)
from app.core.read_models.attention import lease_attention, sr_attention
from app.core.seed.framework import run_seeders

SR_COUNTS = {
    "imported": 25,
    "imported_vip": 6,
    "hardware_unavailable": 10,
    "invited": 30,
    "campus_info_sent": 8,
    "reminder_1_sent": 18,
    "reminder_2_sent": 12,
    "escalated": 8,
    "reschedule_needed": 8,
    "appointment_booked": 35,
    "personalization_in_progress": 12,
    "personalization_done": 14,
    "ready_for_picking": 20,
    "shipped": 15,
    "provided": 15,
    "completed": 54,
    "cancelled": 10,
}
LEASE_COUNTS = {
    "imported": 30,
    "imported_vip": 4,
    "info_sent": 22,
    "reminder_1_sent": 15,
    "reminder_2_sent": 10,
    "escalated": 8,
    "replacement_ordered": 40,
    "probably_closed": 6,
    "closed": 15,
}


async def _counts(session: AsyncSession, model: Any) -> Counter[str]:
    rows = await session.execute(select(model.status, func.count()).group_by(model.status))
    return Counter({str(status): count for status, count in rows.all()})


async def _totals(session: AsyncSession) -> dict[str, int]:
    result = {}
    for model in (ServiceRequest, LeaseReturn, Asset, Contact, User, VipEntry, StatusTransition):
        result[model.__tablename__] = int(
            await session.scalar(select(func.count()).select_from(model)) or 0
        )
    return result


async def test_status_distribution(db_session: AsyncSession) -> None:
    assert await _counts(db_session, ServiceRequest) == Counter(SR_COUNTS)
    assert await _counts(db_session, LeaseReturn) == Counter(LEASE_COUNTS)
    totals = await _totals(db_session)
    assert totals["contacts"] == 420
    assert totals["users"] == 7
    assert totals["vip_entries"] == 8


async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    before = await _totals(db_session)
    await run_seeders(db_session)
    assert await _totals(db_session) == before


CHECKS = {
    "asset invariants": """
        SELECT count(*) FROM assets a JOIN stock_locations l ON l.id = a.location_id WHERE
          (a.status IN ('assigned','prepared','provided') AND a.service_request_id IS NULL)
          OR (a.status = 'provided' AND l.type <> 'in_transit') OR (a.status = 'in_use' AND l.type <> 'in_use')
          OR ((a.status LIKE 'deactivated%') <> (l.type = 'off_fleet'))
          OR (a.installation_place IS NOT NULL AND l.type <> 'staging')
          OR (a.status = 'available' AND l.type NOT IN ('goods_in','staging','goods_out'))
          OR (a.status = 'collected' AND l.type <> 'return')""",
    "ledger ends in current state": """
        SELECT count(*) FROM assets a JOIN LATERAL (SELECT * FROM asset_movements m WHERE m.asset_id = a.id
          ORDER BY occurred_at DESC, id DESC LIMIT 1) m ON true
        WHERE m.to_status <> a.status OR m.to_location_id <> a.location_id""",
    "history ends in current status": """
        SELECT count(*) FROM service_requests s JOIN LATERAL (SELECT * FROM status_transitions t
          WHERE t.entity_type = 'service_request' AND t.entity_id = s.id ORDER BY occurred_at DESC, id DESC LIMIT 1) t
          ON true WHERE t.to_status <> s.status""",
    "no future events": "SELECT count(*) FROM status_transitions WHERE occurred_at > now()",
    "vip flags": """SELECT count(*) FROM service_requests s JOIN contacts c ON c.id = s.contact_id
        WHERE s.is_vip <> EXISTS (SELECT 1 FROM vip_entries v WHERE v.user_id = c.user_id)""",
    "lease tags are asset tags": "SELECT count(*) FROM lease_returns l WHERE NOT EXISTS (SELECT 1 FROM assets a WHERE a.asset_tag = l.asset_tag)",
    "shipped only parcel": "SELECT count(*) FROM service_requests WHERE status = 'shipped' AND coalesce(delivery_type_override, delivery_type) = 'technician'",
    "provided only technician": "SELECT count(*) FROM service_requests WHERE status = 'provided' AND coalesce(delivery_type_override, delivery_type) <> 'technician'",
}


async def test_consistency(db_session: AsyncSession) -> None:
    for name, sql in CHECKS.items():
        assert (await db_session.execute(text(sql))).scalar_one() == 0, name


async def test_story_specifics(db_session: AsyncSession) -> None:
    srs = (await db_session.scalars(select(ServiceRequest))).all()
    flags: Counter[AttentionFlag] = Counter(flag for sr in srs for flag in sr_attention(sr))
    assert flags[AttentionFlag.REGION_UNRESOLVED] == 3
    assert flags[AttentionFlag.UNKNOWN_ARTICLE] >= 4
    assert flags[AttentionFlag.APPOINTMENT_OVERDUE] == 2
    assert flags[AttentionFlag.ESCALATED_STALE] == 3
    assert flags[AttentionFlag.APPOINTMENT_SOON_NOT_READY] >= 3
    assert sum(1 for sr in srs if sr.is_campus) == 15
    paused = [sr for sr in srs if sr.communication_paused or sr.communication_resume_date]
    assert len(paused) == 6
    leases = (await db_session.scalars(select(LeaseReturn))).all()
    lease_flags = Counter(flag for lease in leases for flag in lease_attention(lease))
    assert lease_flags[AttentionFlag.NOT_COLLECTED] == 2
    imported = [lease for lease in leases if lease.status == LeaseReturnStatus.IMPORTED]
    from app.core.timeutil import business_today

    soon = [lease for lease in imported if (lease.retirement_date - business_today()).days <= 42]
    assert len(soon) == 5  # SPEC_RESOLUTIONS #41
    theo = (
        await db_session.scalars(select(User).where(User.email == "theo.technician@example.com"))
    ).one()
    assert theo.is_demo_persona
    today_stops = [
        sr
        for sr in srs
        if sr.status == ServiceRequestStatus.PROVIDED
        and sr.appointment_calendar_name == "Riverton route"
        and sr.appointment_start_at is not None
        and "theo" not in str(sr.id)
    ]
    assert len(today_stops) >= 5


async def test_seed_json_export(db_session: AsyncSession) -> None:
    data = await build_seed_json(db_session)
    assert set(data) == {"meta", "tables", "enums", "config"}
    assert data["meta"]["version"] == 1
    assert len(data["tables"]["service_requests"]) == 300
    assert "passwordHash" not in data["tables"]["users"][0]
    first_sr = data["tables"]["service_requests"][0]
    assert {"number", "statusChangedAt", "appointmentStartAt", "contactId"} <= set(first_sr)
    assert "booking_sandbox_appointments" not in data["tables"]
    assert "serviceRequestStatus" in data["enums"]
    assert data["config"]["appName"] == "Hangar"


async def test_reset_and_seed(db_session: AsyncSession) -> None:
    before = await _totals(db_session)
    await reset_and_seed(db_session)
    after = await _totals(db_session)
    assert after == before
    dana = (
        await db_session.scalars(select(User).where(User.email == "dana.dispatcher@example.com"))
    ).one()
    assert dana.id == 1  # personas keep their ids
