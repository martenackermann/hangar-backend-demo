"""FakeParcelCarrier (default binding, SPEC §8.2): demo labels, tracking on track.example.com."""

from __future__ import annotations

import io
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from reportlab.graphics.barcode import code128
from reportlab.lib.pagesizes import A4, A6
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import open_session
from app.core.gateways.protocols import (
    CarrierError,
    Parcel,
    PostalAddress,
    ShipmentRequest,
    ShipmentResult,
)
from app.core.models import FAKE_CARRIER_SEQ
from app.core.timeutil import business_today

TRACK_BASE = "https://track.example.com"
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class FakeParcelCarrier:
    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._sessions = session_factory or open_session
        self._labels: dict[str, bytes] = {}

    async def _next_number(self) -> int:
        async with self._sessions() as session:
            value = (await session.execute(select(FAKE_CARRIER_SEQ.next_value()))).scalar_one()
        return int(value)

    async def create_shipment(self, request: ShipmentRequest) -> ShipmentResult:
        if request.recipient.zip.strip() == "00000":
            raise CarrierError("Recipient address rejected by carrier")
        if not request.parcels:
            raise CarrierError("A shipment needs at least one parcel", code="carrier_error")
        number = await self._next_number()
        shipment_id = f"PC{business_today():%Y%m%d}{number % 1_000_000:06d}"
        parcel_ids = [f"{shipment_id}-{n}" for n in range(1, len(request.parcels) + 1)]
        return_ids = [
            f"{shipment_id}-{len(parcel_ids) + n}"
            for n in range(1, len(request.return_parcels) + 1)
        ]
        label = render_label(request, shipment_id, parcel_ids, return_ids)
        self._labels[shipment_id] = label
        return ShipmentResult(
            carrier_shipment_id=shipment_id,
            parcel_ids=parcel_ids,
            tracking_urls=[f"{TRACK_BASE}/{pid}" for pid in parcel_ids],
            return_tracking_urls=[f"{TRACK_BASE}/return/{pid}R" for pid in return_ids],
            label_pdf=label,
        )

    async def get_label(self, carrier_shipment_id: str) -> bytes:
        try:
            return self._labels[carrier_shipment_id]
        except KeyError as exc:
            raise CarrierError(f"Unknown shipment {carrier_shipment_id}") from exc


def _address_lines(address: PostalAddress) -> list[str]:
    lines = [address.name]
    if address.name2:
        lines.append(address.name2)
    lines += [address.street, f"{address.zip} {address.city}", address.country]
    return lines


def _draw_label(
    pdf: canvas.Canvas,
    size: tuple[float, float],
    sender: PostalAddress,
    recipient: PostalAddress,
    parcel: Parcel,
    parcel_id: str,
    title: str,
) -> None:
    width, height = size
    margin = 15 * mm
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(margin, height - margin, title)
    pdf.setFont("Helvetica", 9)
    y = height - margin - 10 * mm
    pdf.drawString(margin, y, "From:")
    for line in _address_lines(sender):
        y -= 4.5 * mm
        pdf.drawString(margin + 12 * mm, y, line)
    y -= 9 * mm
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(margin, y, "To:")
    for line in _address_lines(recipient):
        y -= 6 * mm
        pdf.drawString(margin + 12 * mm, y, line)
    pdf.setFont("Helvetica", 9)
    y -= 10 * mm
    pdf.drawString(margin, y, f"Weight: {parcel.weight_kg} kg")
    y -= 5 * mm
    pdf.drawString(margin, y, f"References: {', '.join(parcel.references)}")
    barcode = code128.Code128(parcel_id, barHeight=18 * mm, barWidth=0.4 * mm)
    barcode.drawOn(pdf, margin, y - 28 * mm)
    pdf.drawString(margin, y - 33 * mm, parcel_id)
    pdf.saveState()
    pdf.setFillGray(0.85)
    pdf.setFont("Helvetica-Bold", 26 if width > 300 else 14)
    pdf.translate(width / 2, height / 2)
    pdf.rotate(35)
    pdf.drawCentredString(0, 0, "DEMO LABEL — NOT FOR SHIPPING")
    pdf.restoreState()
    pdf.showPage()


def render_label(
    request: ShipmentRequest, shipment_id: str, parcel_ids: list[str], return_ids: list[str]
) -> bytes:
    size = A4 if request.label_format == "A4" else A6
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=size)
    pdf.setTitle(f"Shipping label {shipment_id}")
    pdf.setAuthor("Acme IT Services (demo)")
    for parcel, parcel_id in zip(request.parcels, parcel_ids, strict=True):
        _draw_label(pdf, size, request.sender, request.recipient, parcel, parcel_id, "Parcel")
    if request.return_address is not None:
        for parcel, parcel_id in zip(request.return_parcels, return_ids, strict=True):
            _draw_label(
                pdf,
                size,
                request.recipient,
                request.return_address,
                parcel,
                parcel_id + "R",
                "Return parcel",
            )
    pdf.save()
    return buffer.getvalue()
