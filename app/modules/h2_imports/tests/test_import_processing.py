"""Import processing per type (SPEC §7.3–§7.7, §6.4, §6.5; RES #29/#37/#49/#55)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from procrastinate.testing import InMemoryConnector
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import commit
from app.core.enums import (
    AssetAction,
    AssetStatus,
    DeliveryType,
    HoldSource,
    ImportJobStatus,
    ImportType,
    ItemSlot,
    LeaseReturnStatus,
    LocationType,
    ServiceRequestStatus,
    Trigger,
)
from app.core.events import ImportFinished, bus
from app.core.files import file_store
from app.core.models import (
    Asset,
    AssetMovement,
    Contact,
    LeaseReturn,
    ServiceRequest,
    StatusTransition,
    StoredFile,
    VipEntry,
)
from app.modules.h2_imports.importing import processor
from app.modules.h2_imports.importing.processor import ImportResult, run_import
from app.modules.h2_imports.importing.runtime import SessionRuntime
from app.modules.h2_imports.models import ImportJob, ImportJobError
from app.testing import factories as f

from .conftest import SR_HEADER, address, csv_bytes, order_details, sr_row

SR = ServiceRequestStatus
LR = LeaseReturnStatus
Run = Callable[..., Awaitable[ImportResult]]

LEASE_HEADER = [
    "asset_tag",
    "serial_number",
    "retirement_date",
    "model",
    "user_id",
    "gender",
    "first_name",
    "last_name",
    "email",
    "cost_center",
]
CONTACT_HEADER = ["user_id", "first_name", "last_name", "email", "zip", "city", "street"]
ASSET_HEADER = ["serial_number", "asset_tag", "model", "mac_address", "checked_in_date"]
SYNC_HEADER = [
    "serial_number",
    "install_state",
    "stockroom",
    "sr_number",
    "assigned_user_id",
    "asset_tag",
    "model",
]


async def sr_by_number(session: AsyncSession, number: str) -> ServiceRequest:
    return (
        await session.scalars(
            select(ServiceRequest)
            .where(ServiceRequest.number == number)
            .execution_options(populate_existing=True)
        )
    ).one()


async def issues(session: AsyncSession, import_job_id: int) -> list[tuple[int, str, str]]:
    rows = (
        await session.scalars(
            select(ImportJobError)
            .where(ImportJobError.import_job_id == import_job_id)
            .order_by(ImportJobError.row_number, ImportJobError.id)
        )
    ).all()
    return [(r.row_number, r.severity, r.code) for r in rows]


def srs(*rows: dict[str, Any]) -> bytes:
    return csv_bytes(SR_HEADER, list(rows))


# --- service requests ----------------------------------------------------------------------


async def test_service_request_create_items_region_contact_and_lease_link(
    db_session: AsyncSession, run_file: Run
) -> None:
    lease = await f.make_lease(db_session, status=LR.INFO_SENT)
    content = srs(
        sr_row(
            "SR0099101",
            user_id="T810001",
            order_details=order_details(
                old_base=lease.asset_tag,
                New_peripheral_1_type="Laptop Ultra 15",
                Software_without_approval="Office Suite; PDF Reader",
            ),
        )
    )
    result = await run_file(content)
    assert result.status == ImportJobStatus.COMPLETED
    assert result.counts["created"] == 1
    assert result.counts["warnings"] == 1
    assert result.post_process["leaseReturnsLinked"] == 1
    assert result.post_process["contactsCreated"] == 1
    assert await issues(db_session, result.import_job_id) == [(2, "warning", "unknown_article")]

    sr = await sr_by_number(db_session, "SR0099101")
    assert sr.status == SR.IMPORTED
    assert sr.region is not None
    assert sr.region.code == "riverton"
    assert sr.delivery_type == DeliveryType.TECHNICIAN
    assert sr.requested_software == ["Office Suite", "PDF Reader"]
    assert sr.last_import_job_id == result.import_job_id
    new_base = sr.item(ItemSlot.NEW_BASE)
    peripheral = sr.item(ItemSlot.NEW_PERIPHERAL_1)
    old_base = sr.item(ItemSlot.OLD_BASE)
    assert new_base is not None
    assert new_base.article is not None
    assert peripheral is not None
    assert peripheral.article_id is None
    assert old_base is not None
    assert old_base.expected_asset_tag == lease.asset_tag
    contact = (await db_session.scalars(select(Contact).where(Contact.user_id == "T810001"))).one()
    assert (contact.last_name, contact.email) == ("Tester", "mia.tester@example.com")
    await db_session.refresh(lease)
    assert lease.status == LR.REPLACEMENT_ORDERED
    assert lease.service_request_id == sr.id
    record = (
        await db_session.scalars(
            select(StatusTransition).where(
                StatusTransition.entity_type == "service_request",
                StatusTransition.entity_id == sr.id,
            )
        )
    ).one()
    assert (record.action, record.trigger) == ("import_create", Trigger.IMPORT)
    assert record.actor_user_id is not None


async def test_vip_campus_hold_cancel_and_unresolved(
    db_session: AsyncSession, run_file: Run
) -> None:
    vip = (await db_session.scalars(select(VipEntry).order_by(VipEntry.id))).first()
    assert vip is not None
    content = srs(
        sr_row("SR0099201", user_id=vip.user_id),
        sr_row("SR0099202", delivery_address=address("1 Campus Way", "10500", "Riverton")),
        sr_row("SR0099203", assignment_group=" rollout hardware HOLD "),
        sr_row("SR0099204", itsm_state="Cancelled"),
        sr_row("SR0099205", delivery_address=address(zip_="99001", city="Nowhere")),
    )
    result = await run_file(content)
    assert result.status == ImportJobStatus.COMPLETED
    assert result.post_process["hardwareHold"] == 1
    assert result.post_process["cancelledByItsm"] == 1
    assert await issues(db_session, result.import_job_id) == [(6, "warning", "region_unresolved")]

    vip_sr = await sr_by_number(db_session, "SR0099201")
    assert (vip_sr.status, vip_sr.is_vip) == (SR.IMPORTED_VIP, True)
    campus = await sr_by_number(db_session, "SR0099202")
    assert campus.is_campus
    assert campus.delivery_type == DeliveryType.TECHNICIAN
    held = await sr_by_number(db_session, "SR0099203")
    assert held.status == SR.HARDWARE_UNAVAILABLE
    assert held.hold_source == HoldSource.IMPORT
    cancelled = await sr_by_number(db_session, "SR0099204")
    assert cancelled.status == SR.CANCELLED
    unresolved = await sr_by_number(db_session, "SR0099205")
    assert unresolved.region_id is None


async def test_service_request_update_empty_cells_and_item_sync(
    db_session: AsyncSession, run_file: Run
) -> None:
    await run_file(srs(sr_row("SR0099301", note_field="x")))
    sr = await sr_by_number(db_session, "SR0099301")
    assert sr.cost_center == "CC-4410"

    second = await run_file(
        srs(
            sr_row(
                "SR0099301",
                cost_center="",
                email="",
                order_details=order_details(new_base="Laptop Performance 16", Note="Call first"),
                delivery_address=address(zip_="20110", city="Hillford"),
            )
        )
    )
    assert second.counts["updated"] == 1
    sr = await sr_by_number(db_session, "SR0099301")
    assert sr.cost_center == "CC-4410"  # empty cell never overwrites (D-20)
    assert sr.note == "Call first"
    assert sr.region is not None
    assert sr.region.code == "hillford"
    new_base = sr.item(ItemSlot.NEW_BASE)
    assert new_base is not None
    assert new_base.requested_type == "Laptop Performance 16"
    assert new_base.article is not None
    assert new_base.article.number == "A-10002"
    contact = sr.contact
    assert contact.email == "mia.tester@example.com"

    third = await run_file(
        srs(
            sr_row(
                "SR0099301",
                order_details=order_details(new_base="Laptop Performance 16", Note="Call first"),
                delivery_address=address(zip_="20110", city="Hillford"),
            )
        )
    )
    assert (third.counts["unchanged"], third.counts["updated"]) == (1, 0)


async def test_items_locked_after_appointment(db_session: AsyncSession, run_file: Run) -> None:
    sr = await f.make_sr(db_session, status=SR.APPOINTMENT_BOOKED)
    result = await run_file(
        srs(
            sr_row(
                sr.number,
                user_id=sr.contact.user_id,
                order_details=order_details(new_base="Tablet 11"),
            )
        )
    )
    assert result.status == ImportJobStatus.COMPLETED
    assert await issues(db_session, result.import_job_id) == [(2, "warning", "items_locked")]
    sr = await sr_by_number(db_session, sr.number)
    new_base = sr.item(ItemSlot.NEW_BASE)
    assert new_base is not None
    assert new_base.requested_type == "Laptop Standard 14"
    assert sr.status == SR.APPOINTMENT_BOOKED


async def test_errors_report_mail_and_import_finished(
    db_session: AsyncSession, run_file: Run, jobs: InMemoryConnector
) -> None:
    seen: list[ImportFinished] = []

    async def capture(_session: AsyncSession, event: ImportFinished) -> None:
        seen.append(event)

    bus.subscribe(ImportFinished, capture, phase="in_transaction")
    try:
        result = await run_file(
            srs(
                sr_row("SR0099401"),
                sr_row("SR12AB", task_number=""),
                sr_row("SR0099401"),
            )
        )
    finally:
        bus.unsubscribe(ImportFinished, capture)
    assert result.status == ImportJobStatus.COMPLETED_WITH_ERRORS
    assert result.counts == {
        "total": 3,
        "processed": 3,
        "created": 1,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "errors": 2,
        "warnings": 0,
    }
    assert [(e.type, e.status) for e in seen] == [
        (ImportType.SERVICE_REQUESTS, ImportJobStatus.COMPLETED_WITH_ERRORS)
    ]
    job = await db_session.get(ImportJob, result.import_job_id)
    assert job is not None
    assert job.error_report_file_id is not None
    report_file = await db_session.get(StoredFile, job.error_report_file_id)
    assert report_file is not None
    report = file_store().read(report_file.storage_key).decode("utf-8-sig").splitlines()
    assert report[0].endswith("row_number,severity,column,code,message")
    assert ",3,error,sr_number,invalid_format," in report[1]
    assert ",4,error,sr_number,duplicate_in_file," in report[2]
    await commit(db_session)
    mails = [j for j in jobs.jobs.values() if j["task_name"] == "notifications.send"]
    summary = [m for m in mails if m["args"]["template_id"] == "import.summary"]
    assert len(summary) == 1
    assert summary[0]["args"]["to"] == ["dana.dispatcher@example.com"]
    assert summary[0]["args"]["attachment_file_ids"] == [job.error_report_file_id]


async def test_all_rows_failing_is_failed(run_file: Run) -> None:
    result = await run_file(srs(sr_row("SR12AB", task_number="")))
    assert result.status == ImportJobStatus.FAILED


async def test_dry_run_changes_nothing(db_session: AsyncSession, run_file: Run) -> None:
    seen: list[ImportFinished] = []

    async def capture(_session: AsyncSession, event: ImportFinished) -> None:
        seen.append(event)

    bus.subscribe(ImportFinished, capture, phase="in_transaction")
    try:
        result = await run_file(
            srs(sr_row("SR0099501"), sr_row("SR0099502", assignment_group="Rollout Hardware Hold")),
            options={"dryRun": True},
        )
    finally:
        bus.unsubscribe(ImportFinished, capture)
    assert result.status == ImportJobStatus.COMPLETED
    assert result.counts["created"] == 2
    assert result.post_process["hardwareHold"] == 1
    assert seen == []
    found = await db_session.scalar(
        select(ServiceRequest.id).where(ServiceRequest.number.in_(["SR0099501", "SR0099502"]))
    )
    assert found is None


async def test_cancel_between_chunks(
    db_session: AsyncSession, upload: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processor, "CHUNK_SIZE", 2)
    rows = [{"user_id": f"T82000{i}", "last_name": f"Person{i}"} for i in range(5)]
    response = await upload(csv_bytes(["user_id", "last_name"], rows))
    import_job_id = response.json()["importJobId"]

    class CancellingRuntime(SessionRuntime):
        async def cancel_requested(self, import_job_id: int) -> bool:
            return True

    result = await run_import(CancellingRuntime(db_session), import_job_id)
    assert result.status == ImportJobStatus.CANCELLED
    assert result.counts["processed"] == 2
    assert result.counts["created"] == 2


# --- lease returns -------------------------------------------------------------------------


def lease_row(tag: str, user_id: str = "T830001", **overrides: Any) -> dict[str, Any]:
    row = {
        "asset_tag": tag,
        "serial_number": "KD" + tag[2:] + "XX",
        "retirement_date": "2026-12-01",
        "model": "Aero 14 G2 Laptop",
        "user_id": user_id,
        "gender": "m",
        "first_name": "Noah",
        "last_name": "Leaser",
        "email": "noah.leaser@example.com",
        "cost_center": "CC-1200",
    }
    row.update(overrides)
    return row


async def test_lease_import_create_update_reopen_mark_missing_and_late_link(
    db_session: AsyncSession, run_file: Run
) -> None:
    existing = await f.make_lease(db_session, status=LR.INFO_SENT)
    reappearing = await f.make_lease(
        db_session, status=LR.PROBABLY_CLOSED, info_sent_at=f.now_utc()
    )
    missing = await f.make_lease(db_session, status=LR.IMPORTED)
    sr = await f.make_sr(db_session, status=SR.INVITED, old_tags=("GX955501",))
    content = csv_bytes(
        LEASE_HEADER,
        [
            lease_row("GX955501"),
            lease_row(existing.asset_tag, user_id=existing.user_id, model="Aero 16 G2 Laptop"),
            lease_row(reappearing.asset_tag, user_id=reappearing.user_id, email=""),
        ],
    )
    result = await run_file(content, options={"markMissingAsProbablyClosed": True})
    assert result.status == ImportJobStatus.COMPLETED
    assert (result.counts["created"], result.counts["updated"]) == (1, 2)
    assert result.post_process["reopened"] == 1
    assert result.post_process["leaseReturnsLinked"] == 1
    assert result.post_process["probablyClosed"] >= 1

    late = (
        await db_session.scalars(select(LeaseReturn).where(LeaseReturn.asset_tag == "GX955501"))
    ).one()
    assert late.status == LR.REPLACEMENT_ORDERED
    assert late.service_request_id == sr.id
    await db_session.refresh(existing)
    assert existing.model == "Aero 16 G2 Laptop"
    assert existing.status == LR.INFO_SENT
    await db_session.refresh(reappearing)
    assert reappearing.status == LR.INFO_SENT
    await db_session.refresh(missing)
    assert missing.status == LR.PROBABLY_CLOSED


# --- contacts ------------------------------------------------------------------------------


async def test_contacts_import_and_lease_campus_recompute(
    db_session: AsyncSession, run_file: Run
) -> None:
    lease = await f.make_lease(db_session, status=LR.IMPORTED)
    assert lease.contact is not None
    assert not lease.is_campus
    content = csv_bytes(
        CONTACT_HEADER,
        [
            {"user_id": "t840001", "last_name": "Newcomer", "zip": "2011", "city": "Hillford"},
            {
                "user_id": lease.contact.user_id,
                "last_name": lease.contact.last_name,
                "zip": "10500",
                "city": "Riverton",
                "street": "1 Campus Way",
            },
            {"user_id": "T840003", "last_name": "Zipper", "zip": "ABCDE"},
        ],
    )
    result = await run_file(content)
    assert result.status == ImportJobStatus.COMPLETED_WITH_ERRORS
    assert (result.counts["created"], result.counts["updated"], result.counts["errors"]) == (
        1,
        1,
        1,
    )
    new = (await db_session.scalars(select(Contact).where(Contact.user_id == "T840001"))).one()
    assert new.zip == "02011"
    await db_session.refresh(lease)
    assert lease.is_campus is True
    assert (4, "error", "invalid_format") in await issues(db_session, result.import_job_id)


# --- assets_new ----------------------------------------------------------------------------


async def test_assets_new_receive_and_errors(db_session: AsyncSession, run_file: Run) -> None:
    goods_in = await f.location(db_session, "RIV-GOODS_IN")
    available = await f.make_asset(db_session, location_code="RIV-STAGING", place="Z9-02")
    assigned = await f.make_asset(db_session, status=AssetStatus.ASSIGNED)
    other = await f.make_asset(db_session)
    content = csv_bytes(
        ASSET_HEADER,
        [
            {"serial_number": "kdnew0001", "asset_tag": "GX977001", "model": "kd-aero14-g3"},
            {"serial_number": available.serial_number, "model": "KD-AERO14-G3"},
            {"serial_number": assigned.serial_number, "model": "KD-AERO14-G3"},
            {"serial_number": "KDNEW0004", "asset_tag": other.asset_tag, "model": "KD-AERO14-G3"},
            {"serial_number": "KDNEW0005", "model": "KD-UNKNOWN-9", "mac_address": "zz"},
        ],
    )
    result = await run_file(content, options={"targetLocationId": goods_in.id})
    assert result.status == ImportJobStatus.COMPLETED_WITH_ERRORS
    assert (result.counts["created"], result.counts["updated"], result.counts["errors"]) == (
        2,
        1,
        2,
    )
    found = await issues(db_session, result.import_job_id)
    assert (4, "error", "asset_in_use") in found
    assert (5, "error", "asset_tag_conflict") in found
    assert (6, "warning", "unknown_model") in found
    assert (6, "warning", "invalid_format") in found

    asset = (
        await db_session.scalars(select(Asset).where(Asset.serial_number == "KDNEW0001"))
    ).one()
    assert (asset.status, asset.location_id) == (AssetStatus.AVAILABLE, goods_in.id)
    assert asset.article is not None
    assert asset.article.number == "A-10001"
    movement = (
        await db_session.scalars(select(AssetMovement).where(AssetMovement.asset_id == asset.id))
    ).one()
    assert movement.action == AssetAction.RECEIVE
    assert movement.source_ref == f"import:{result.import_job_id}"
    await db_session.refresh(available)
    assert available.location_id == goods_in.id
    assert available.installation_place is None


async def test_assets_new_requires_target_location(upload: Any) -> None:
    content = csv_bytes(ASSET_HEADER, [{"serial_number": "KDNEW0001", "model": "KD-AERO14-G3"}])
    response = await upload(content)
    assert response.status_code == 422
    assert "options.targetLocationId" in response.json()["errors"]


# --- asset status sync ---------------------------------------------------------------------


async def test_asset_status_sync(db_session: AsyncSession, run_file: Run) -> None:
    sr = await f.make_sr(db_session, status=SR.INVITED, old_tags=("TT990001",))
    in_use = await f.make_asset(db_session, status=AssetStatus.AVAILABLE)
    in_use.asset_tag = "TT990001"
    await db_session.flush()
    assigned = await f.make_asset(db_session, status=AssetStatus.ASSIGNED)
    contact = await f.make_contact(db_session)
    content = csv_bytes(
        SYNC_HEADER,
        [
            {
                "serial_number": "KDSYNC0001",
                "install_state": "In use",
                "assigned_user_id": contact.user_id,
                "model": "KD-AERO14-G2",
            },
            {
                "serial_number": in_use.serial_number,
                "install_state": "In stock",
                "stockroom": "Old device stockroom",
                "sr_number": sr.number,
            },
            {"serial_number": assigned.serial_number, "install_state": "In use"},
            {"serial_number": "KDSYNC0004", "install_state": "Retired"},
            {
                "serial_number": "KDSYNC0005",
                "install_state": "In transit",
                "sr_number": "SR0000001",
            },
            {"serial_number": "KDSYNC0006", "install_state": "In use", "assigned_user_id": "X999"},
        ],
    )
    result = await run_file(content)
    assert result.status == ImportJobStatus.COMPLETED_WITH_ERRORS
    counts = result.counts
    assert (counts["created"], counts["updated"], counts["skipped"], counts["errors"]) == (
        3,
        1,
        1,
        1,
    )
    found = await issues(db_session, result.import_job_id)
    assert (4, "error", "asset_in_workflow") in found
    assert (5, "warning", "unmapped_state") in found
    assert (6, "warning", "sr_not_found") in found
    assert (7, "warning", "unknown_contact") in found

    created = (
        await db_session.scalars(select(Asset).where(Asset.serial_number == "KDSYNC0001"))
    ).one()
    assert created.status == AssetStatus.IN_USE
    assert created.assigned_contact_id == contact.id
    collected = await db_session.get(Asset, in_use.id, populate_existing=True)
    assert collected is not None
    assert collected.status == AssetStatus.COLLECTED
    assert collected.location.type == LocationType.RETURN
    sr = await sr_by_number(db_session, sr.number)
    old = sr.item(ItemSlot.OLD_BASE)
    assert old is not None
    assert old.asset_id == in_use.id
    transit = (
        await db_session.scalars(select(Asset).where(Asset.serial_number == "KDSYNC0005"))
    ).one()
    assert transit.location.code == "RIV-RETURN"  # no SR → Riverton returns


async def test_status_sync_rejects_deactivated(db_session: AsyncSession, run_file: Run) -> None:
    asset = await f.make_asset(db_session)
    asset.status = AssetStatus.DEACTIVATED_RETURNED
    asset.location = await f.location(db_session, "OFF_FLEET")
    asset.installation_place = None
    await db_session.flush()
    content = csv_bytes(
        SYNC_HEADER, [{"serial_number": asset.serial_number, "install_state": "In use"}]
    )
    result = await run_file(content)
    assert result.status == ImportJobStatus.FAILED
    assert await issues(db_session, result.import_job_id) == [(2, "error", "asset_deactivated")]


async def test_fresh_session_creates_requests_and_leases(
    db_session: AsyncSession, upload: Any, process: Any
) -> None:
    """Regression: the worker's session starts empty — lookups must not autoflush new rows."""
    sr_job = (await upload(srs(sr_row("SR0099901")))).json()["importJobId"]
    lease_job = (await upload(csv_bytes(LEASE_HEADER, [lease_row("GX955901")]))).json()[
        "importJobId"
    ]
    db_session.expunge_all()
    assert (await process(sr_job)).counts["created"] == 1
    db_session.expunge_all()
    assert (await process(lease_job)).counts["created"] == 1
