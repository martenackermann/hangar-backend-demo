"""Handover protocol PDF (SPEC §6.11): ReportLab, A4, two pages.

Page 1 — data deletion consent for the collected old devices; page 2 — hardware receipt
for the delivered new devices. Stored as ``stored_files.purpose = protocol`` and linked via
``service_requests.protocol_file_id``. No drawn signature (decision D-19): printed names and
signature lines only.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AssetStatus, ItemSlot, OsReinstallTiming, label
from app.core.errors import ValidationProblem
from app.core.files import file_store, store_file
from app.core.loading import ensure_loaded
from app.core.models import ServiceRequest, StatusTransition, StoredFile, User
from app.core.notifications import notify
from app.core.notifications.renderer import format_datetime
from app.core.schemas import EntityRef
from app.core.timeutil import now_utc

from .common import is_new_slot

OPERATOR = "Acme IT Services"
CLIENT = "Globex Fleet AG"
COMPLETION_ACTIONS = ("complete_deployment", "complete_manually")


@dataclass
class ProtocolData:
    sr_number: str
    task_number: str | None
    user_name: str
    user_id: str
    completed_at: datetime
    recipient_name: str
    technician_name: str
    os_reinstall_on_pickup: bool
    collected: list[tuple[str, str, str, str]] = field(default_factory=list)
    delivered: list[tuple[str, str, str, str]] = field(default_factory=list)
    software: list[str] = field(default_factory=list)
    deviation: tuple[str, str, str | None] | None = None
    address: str = ""


async def protocol_data(session: AsyncSession, sr: ServiceRequest) -> ProtocolData:
    await ensure_loaded(session, sr)
    record = (
        await session.scalars(
            select(StatusTransition)
            .where(
                StatusTransition.entity_type == "service_request",
                StatusTransition.entity_id == sr.id,
                StatusTransition.action.in_(COMPLETION_ACTIONS),
            )
            .order_by(StatusTransition.occurred_at.desc(), StatusTransition.id.desc())
        )
    ).first()
    payload = (record.payload or {}) if record is not None else {}
    technician = (
        await session.get(User, record.actor_user_id)
        if record is not None and record.actor_user_id is not None
        else None
    )
    contact = sr.contact
    collected: list[tuple[str, str, str, str]] = []
    delivered: list[tuple[str, str, str, str]] = []
    for item in sr.items:
        asset = item.asset
        slot = label(ItemSlot(item.slot), ItemSlot)
        if is_new_slot(item.slot):
            if asset is None or (item.verified_at is None and asset.status != AssetStatus.IN_USE):
                continue
            article = asset.article.name if asset.article else (item.requested_type or "Device")
            delivered.append((slot, article, asset.asset_tag or "—", asset.serial_number))
        elif item.verified_at is not None and asset is not None:
            model = asset.article.name if asset.article else (asset.model or "Device")
            collected.append((slot, asset.asset_tag or "—", asset.serial_number, model))
    code = sr.deviation_code
    address = ", ".join(
        part
        for part in (
            sr.delivery_street,
            f"{sr.delivery_zip or ''} {sr.delivery_city or ''}".strip(),
        )
        if part
    )
    return ProtocolData(
        sr_number=sr.number,
        task_number=sr.task_number,
        user_name=contact.display_name,
        user_id=contact.user_id,
        completed_at=sr.completed_at or (record.occurred_at if record else now_utc()),
        recipient_name=str(payload.get("recipientName") or contact.display_name),
        technician_name=technician.display_name if technician else "Field technician",
        os_reinstall_on_pickup=sr.os_reinstall == OsReinstallTiming.ON_PICKUP,
        collected=collected,
        delivered=delivered,
        software=list(sr.installed_software or []),
        deviation=(code.code, code.label, sr.deviation_comment) if code else None,
        address=address,
    )


# --- rendering --------------------------------------------------------------------------------

_ACCENT = colors.HexColor("#1f3a5f")
_GRID = colors.HexColor("#c9d1db")


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "brand": ParagraphStyle(
            "brand",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=15,
            textColor=_ACCENT,
        ),
        "sub": ParagraphStyle("sub", parent=base["Normal"], fontSize=9, textColor=colors.grey),
        "h1": ParagraphStyle(
            "h1", parent=base["Heading1"], fontSize=16, spaceBefore=10, spaceAfter=6
        ),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=11.5, spaceBefore=10),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontSize=10, leading=14, alignment=TA_LEFT
        ),
        "small": ParagraphStyle("small", parent=base["Normal"], fontSize=8.5, leading=11),
    }


def _table(rows: list[list[str]], widths: list[float]) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
                ("GRID", (0, 0), (-1, -1), 0.5, _GRID),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _facts(rows: list[tuple[str, str]]) -> Table:
    table = Table([[k, v] for k, v in rows], colWidths=[40 * mm, 130 * mm])
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def _signatures(left: tuple[str, str], right: tuple[str, str]) -> Table:
    rows = [
        ["", "", ""],
        ["_" * 38, "", "_" * 38],
        [f"{left[0]}: {left[1]}", "", f"{right[0]}: {right[1]}"],
        ["Date, signature", "", "Date, signature"],
    ]
    table = Table(
        rows, colWidths=[78 * mm, 14 * mm, 78 * mm], rowHeights=[16 * mm, 6 * mm, 6 * mm, 5 * mm]
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TEXTCOLOR", (0, 3), (-1, 3), colors.grey),
                ("FONTSIZE", (0, 3), (-1, 3), 8),
            ]
        )
    )
    return table


def _header(data: ProtocolData, title: str, styles: dict[str, ParagraphStyle]) -> list[object]:
    return [
        Paragraph(OPERATOR, styles["brand"]),
        Paragraph(f"Hardware rollout on behalf of {CLIENT} (client)", styles["sub"]),
        Paragraph(title, styles["h1"]),
        _facts(
            [
                ("Service request", data.sr_number),
                ("Task", data.task_number or "—"),
                ("User", f"{data.user_name} ({data.user_id})"),
                ("Address", data.address or "—"),
                ("Date", format_datetime(data.completed_at)),
            ]
        ),
        Spacer(1, 6 * mm),
    ]


def render_protocol_pdf(data: ProtocolData) -> bytes:
    """Two A4 pages; returns PDF bytes."""
    styles = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Handover protocol {data.sr_number}",
        author=OPERATOR,
    )

    def footer(canvas: object, document: object) -> None:
        canvas.saveState()  # type: ignore[attr-defined]
        canvas.setFont("Helvetica", 8)  # type: ignore[attr-defined]
        canvas.setFillColor(colors.grey)  # type: ignore[attr-defined]
        page = canvas.getPageNumber()  # type: ignore[attr-defined]
        canvas.drawString(  # type: ignore[attr-defined]
            20 * mm,
            10 * mm,
            f"{OPERATOR} · {data.sr_number} · Handover protocol · page {page} of 2 · demo document",
        )
        canvas.restoreState()  # type: ignore[attr-defined]

    story: list[object] = []
    # --- page 1: data deletion consent ------------------------------------------------------
    story += _header(data, "Data deletion consent (old devices)", styles)
    story.append(Paragraph("Collected devices", styles["h2"]))
    collected_rows = [["Slot", "Asset tag", "Serial number", "Model"]] + (
        [list(row) for row in data.collected] or [["—", "No old devices collected", "", ""]]
    )
    story.append(_table(collected_rows, [38 * mm, 32 * mm, 45 * mm, 55 * mm]))
    story.append(Spacer(1, 5 * mm))
    story.append(
        Paragraph(
            "I confirm that my personal data on the devices listed above has been backed up or "
            "is no longer needed. I agree that "
            f"{OPERATOR} securely wipes these devices on behalf of {CLIENT} before they are "
            "returned to the lessor or leave the fleet. Data that remains on the devices after "
            "the handover cannot be restored.",
            styles["body"],
        )
    )
    if data.os_reinstall_on_pickup:
        story.append(Spacer(1, 3 * mm))
        story.append(
            Paragraph(
                "<b>OS reinstall on pickup:</b> the old device is wiped and its operating system "
                "is reinstalled when it is collected.",
                styles["body"],
            )
        )
    story.append(Spacer(1, 10 * mm))
    story.append(_signatures(("User", data.user_name), ("Technician", data.technician_name)))
    story.append(PageBreak())

    # --- page 2: hardware receipt -----------------------------------------------------------
    story += _header(data, "Hardware receipt", styles)
    story.append(Paragraph("Delivered devices", styles["h2"]))
    delivered_rows = [["Slot", "Article", "Asset tag", "Serial number"]] + (
        [list(row) for row in data.delivered] or [["—", "No new devices delivered", "", ""]]
    )
    story.append(_table(delivered_rows, [38 * mm, 55 * mm, 32 * mm, 45 * mm]))
    story.append(Paragraph("Installed software", styles["h2"]))
    story.append(
        Paragraph(
            escape(", ".join(data.software)) if data.software else "No additional software.",
            styles["body"],
        )
    )
    if data.deviation is not None:
        code, text, comment = data.deviation
        story.append(Paragraph("Deviation", styles["h2"]))
        story.append(
            Paragraph(
                # Paragraph text is markup: user text must not inject tags (<img src=...>).
                f"<b>{escape(code)}</b> {escape(text)}"
                + (f"<br/>{escape(comment)}" if comment else ""),
                styles["body"],
            )
        )
    story.append(Spacer(1, 4 * mm))
    story.append(
        _facts(
            [
                ("Recipient", data.recipient_name),
                ("Technician", data.technician_name),
                ("Completed", format_datetime(data.completed_at)),
            ]
        )
    )
    story.append(Spacer(1, 3 * mm))
    story.append(
        Paragraph(
            "The recipient confirms that the devices listed above were handed over complete and "
            "in working order.",
            styles["small"],
        )
    )
    story.append(Spacer(1, 8 * mm))
    story.append(
        _signatures(("Recipient", data.recipient_name), ("Technician", data.technician_name))
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)  # type: ignore[arg-type]
    return buffer.getvalue()


# --- storage + mail ---------------------------------------------------------------------------


async def generate_protocol(
    session: AsyncSession, sr: ServiceRequest, *, created_by: User | int | None = None
) -> StoredFile:
    data = await protocol_data(session, sr)
    pdf = render_protocol_pdf(data)
    stored = await store_file(
        session,
        pdf,
        f"protocol-{sr.number}.pdf",
        "application/pdf",
        "protocol",
        entity=EntityRef(type="service_request", id=sr.id, label=sr.number),
        created_by=created_by,
    )
    sr.protocol_file_id = stored.id
    return stored


async def ensure_protocol(
    session: AsyncSession, sr: ServiceRequest, *, created_by: User | int | None = None
) -> StoredFile:
    """The stored protocol, regenerated when it is missing (SPEC §6.11)."""
    if sr.protocol_file_id is not None:
        stored = await session.get(StoredFile, sr.protocol_file_id)
        if stored is not None and file_store().exists(stored.storage_key):
            return stored
    return await generate_protocol(session, sr, created_by=created_by)


async def send_protocol(
    session: AsyncSession,
    sr: ServiceRequest,
    *,
    email: str | None,
    actor: User | None,
) -> str:
    """Queue ``sr.protocol`` with the PDF attached; returns the notification id."""
    await ensure_loaded(session, sr)
    recipient = email or (sr.contact.email if sr.contact else None)
    if not recipient:
        raise ValidationProblem({"email": ["The contact has no email address. Enter one."]})
    stored = await ensure_protocol(session, sr, created_by=actor)
    data = await protocol_data(session, sr)
    return await notify(
        session,
        "sr.protocol",
        to=[recipient],
        context=_mail_context(sr, data),
        entity=EntityRef(type="service_request", id=sr.id, label=sr.number),
        attachments=[stored.id],
        actor=actor,
    )


async def protocol_context(session: AsyncSession, entity_id: int) -> dict[str, object]:
    """``build_context`` for template previews (SPEC_RESOLUTIONS #13)."""
    sr = await session.get(ServiceRequest, entity_id)
    if sr is None:
        return dict(PROTOCOL_SAMPLE)
    return _mail_context(sr, await protocol_data(session, sr))


def _mail_context(sr: ServiceRequest, data: ProtocolData) -> dict[str, object]:
    contact = sr.contact
    return {
        "sr_number": sr.number,
        "gender": str(contact.gender),
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "completed_at": data.completed_at.isoformat(),
        "recipient_name": data.recipient_name,
        "technician_name": data.technician_name,
        "delivered": [{"slot": d[0], "article": d[1], "asset_tag": d[2]} for d in data.delivered],
        "collected": [{"slot": c[0], "asset_tag": c[1], "model": c[3]} for c in data.collected],
        "deviation": {"code": data.deviation[0], "label": data.deviation[1]}
        if data.deviation
        else None,
    }


PROTOCOL_SAMPLE: dict[str, object] = {
    "sr_number": "SR0012345",
    "gender": "female",
    "first_name": "Mia",
    "last_name": "Turner",
    "completed_at": "2026-10-13T09:30:00Z",
    "recipient_name": "Mia Turner",
    "technician_name": "Theo Technician",
    "delivered": [
        {"slot": "New base device", "article": "Aero 14 G3 Laptop", "asset_tag": "GX204811"},
        {"slot": "New peripheral 1", "article": "Dock U4", "asset_tag": "GX204812"},
    ],
    "collected": [
        {"slot": "Old base device", "asset_tag": "GX101877", "model": "Aero 14 G2 Laptop"}
    ],
    "deviation": None,
}
