"""Call-off order documents: XLSX (xlsxwriter) and PDF (ReportLab) — SPEC §6.8."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime

import xlsxwriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.config import get_settings
from app.core.notifications.renderer import CLIENT, OPERATOR, format_date, format_datetime
from app.core.spreadsheets import XLSX_WORKBOOK_OPTIONS

PARTNER = "Initech Logistics"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_TYPE = "application/pdf"

XLSX_COLUMNS = (
    ("Article number", 14),
    ("External part no.", 18),
    ("Name", 34),
    ("Model number", 18),
    ("Quantity", 10),
    ("Demand", 10),
    ("Stock", 10),
    ("Buffer", 10),
)


@dataclass(frozen=True, slots=True)
class DocLine:
    article_number: str
    external_part_number: str | None
    name: str
    model_number: str
    quantity: int
    demand: int
    stock: int
    buffer: int


@dataclass(frozen=True, slots=True)
class OrderDocument:
    number: str
    status_label: str
    site_name: str
    site_address: str
    delivery_date: date
    recipient_email: str
    note: str | None
    created_by: str
    created_at: datetime
    sent_at: datetime | None
    lines: list[DocLine]

    @property
    def total_quantity(self) -> int:
        return sum(line.quantity for line in self.lines)


def order_xlsx(doc: OrderDocument) -> bytes:
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {**XLSX_WORKBOOK_OPTIONS, "in_memory": True})
    bold = workbook.add_format({"bold": True, "bg_color": "#E9ECEF", "border": 1})
    number = workbook.add_format({"num_format": "0"})
    sheet = workbook.add_worksheet("Lines")
    for col, (title, width) in enumerate(XLSX_COLUMNS):
        sheet.write(0, col, title, bold)
        sheet.set_column(col, col, width)
    for row, line in enumerate(doc.lines, start=1):
        sheet.write_string(row, 0, line.article_number)
        sheet.write_string(row, 1, line.external_part_number or "")
        sheet.write_string(row, 2, line.name)
        sheet.write_string(row, 3, line.model_number)
        sheet.write_number(row, 4, line.quantity, number)
        sheet.write_number(row, 5, line.demand, number)
        sheet.write_number(row, 6, line.stock, number)
        sheet.write_number(row, 7, line.buffer, number)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, max(len(doc.lines), 1), len(XLSX_COLUMNS) - 1)

    meta = workbook.add_worksheet("Order")
    meta.set_column(0, 0, 20)
    meta.set_column(1, 1, 50)
    facts = [
        ("Order number", doc.number),
        ("Status", doc.status_label),
        ("From", f"{OPERATOR} (for {CLIENT})"),
        ("To", f"{PARTNER} <{doc.recipient_email}>"),
        ("Deliver to", f"{doc.site_name}, {doc.site_address}"),
        ("Delivery date", format_date(doc.delivery_date)),
        ("Total quantity", str(doc.total_quantity)),
        ("Note", doc.note or ""),
        ("Created", f"{format_datetime(doc.created_at)} by {doc.created_by}"),
        ("Sent", format_datetime(doc.sent_at) if doc.sent_at else "not sent yet"),
    ]
    for row, (key, value) in enumerate(facts):
        meta.write(row, 0, key, bold)
        meta.write_string(row, 1, value)
    workbook.close()
    return buffer.getvalue()


def order_pdf(doc: OrderDocument) -> bytes:
    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Call-off order {doc.number}",
        author=OPERATOR,
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5, leading=11)
    story: list = [
        Paragraph(f"{OPERATOR} · on behalf of {CLIENT}", small),
        Spacer(1, 4 * mm),
        Paragraph(f"Call-off order {doc.number}", styles["Title"]),
        Paragraph(f"To {PARTNER} · {_escape(doc.recipient_email)}", styles["Normal"]),
        Spacer(1, 4 * mm),
    ]
    facts = [
        ["Deliver to", f"{doc.site_name}, {doc.site_address}"],
        ["Delivery date", format_date(doc.delivery_date)],
        ["Status", doc.status_label],
        ["Created", f"{format_datetime(doc.created_at)} by {doc.created_by}"],
        ["Sent", format_datetime(doc.sent_at) if doc.sent_at else "not sent yet"],
    ]
    fact_table = Table(facts, colWidths=[32 * mm, 140 * mm])
    fact_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story += [fact_table, Spacer(1, 6 * mm)]
    header = ["Article no.", "Part no.", "Name", "Model", "Qty"]
    body = [
        [
            line.article_number,
            line.external_part_number or "",
            Paragraph(_escape(line.name), small),  # Paragraph text is markup
            line.model_number,
            str(line.quantity),
        ]
        for line in doc.lines
    ]
    total = ["", "", "", "Total", str(doc.total_quantity)]
    table = Table(
        [header, *body, total],
        colWidths=[24 * mm, 30 * mm, 62 * mm, 40 * mm, 16 * mm],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9ECEF")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#ADB5BD")),
                ("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.HexColor("#ADB5BD")),
                ("ALIGN", (4, 0), (4, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(table)
    if doc.note:
        story += [
            Spacer(1, 5 * mm),
            Paragraph(f"<b>Note:</b> {_escape(doc.note)}", styles["Normal"]),
        ]
    story += [
        Spacer(1, 8 * mm),
        Paragraph(
            f"Questions: {_escape(get_settings().mail_team_mailbox)}. "
            + (
                "This is a demo system; all names and addresses are fictional."
                if get_settings().demo_mode
                else ""
            ),
            small,
        ),
    ]
    pdf.build(story)
    return buffer.getvalue()


def _escape(value: str) -> str:
    """ReportLab ``Paragraph`` text is mini-markup (``<img src>``, ``<a href>``, ...)."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
