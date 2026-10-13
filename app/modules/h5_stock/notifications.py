"""H5 notification templates (SPEC §10): ``calloff.order`` and ``inventory.report``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.notifications import NotificationTemplate

TEMPLATES = Path(__file__).parent / "templates"

CALLOFF_SAMPLE: dict[str, Any] = {
    "order_number": "CO-EAS-20261013-01",
    "site_name": "Eastgate Hub",
    "site_address": "88 Dock Road, 30030 Eastgate",
    "delivery_date": "2026-10-16",
    "note": "Please deliver to the goods-in ramp before 10:00.",
    "sender_name": "Dana Dispatcher",
    "total_quantity": 14,
    "lines": [
        {
            "article_number": "A-10204",
            "external_part_number": "IL-10204-LM",
            "name": "View 34W Curved Monitor",
            "model_number": "LM-VIEW-34W",
            "quantity": 8,
        },
        {
            "article_number": "A-10102",
            "external_part_number": "IL-10102-KD",
            "name": "Dock T5 Pro",
            "model_number": "KD-DOCK-T5P",
            "quantity": 6,
        },
    ],
}

INVENTORY_SAMPLE: dict[str, Any] = {
    "inventory_id": 3,
    "site_name": "Hillford Hub",
    "count_date": "2026-10-13",
    "expected": 182,
    "matched": 171,
    "missing": 7,
    "wrong_location": 3,
    "unknown": 2,
    "corrected": 10,
    "apply_corrections": True,
    "closed_by": "Dana Dispatcher",
    "opened_at": "2026-10-12T07:00:00Z",
}


async def calloff_context(session: AsyncSession, entity_id: int) -> dict[str, Any]:
    from .models import CallOffOrder
    from .services.calloff import document_for, mail_context

    order = await session.get(CallOffOrder, entity_id)
    if order is None:
        return dict(CALLOFF_SAMPLE)
    return mail_context(order, await document_for(session, order))


async def inventory_context(session: AsyncSession, entity_id: int) -> dict[str, Any]:
    from .models import Inventory
    from .services.inventory import report_context, summary_counts

    inventory = await session.get(Inventory, entity_id)
    if inventory is None:
        return dict(INVENTORY_SAMPLE)
    counts = await summary_counts(session, inventory)
    actor = inventory.closer or inventory.opener
    return report_context(inventory, counts, actor)


CALLOFF_ORDER = NotificationTemplate(
    id="calloff.order",
    owner="h5",
    name="Call-off order",
    subject="Call-off order {{ order_number }} – {{ site_name }} – delivery {{ delivery_date | date }}",
    html=TEMPLATES / "calloff.order.html.j2",
    text=TEMPLATES / "calloff.order.txt.j2",
    description="Replenishment order to Initech Logistics with the order XLSX and PDF attached.",
    trigger="Call-off order sent",
    recipients="Partner order mailbox (cc team mailbox)",
    entity_type="calloff_order",
    sample_context=CALLOFF_SAMPLE,
    build_context=calloff_context,
)

INVENTORY_REPORT = NotificationTemplate(
    id="inventory.report",
    owner="h5",
    name="Inventory report",
    subject="Inventory {{ site_name }} closed: {{ missing }} missing, {{ wrong_location }} misplaced",
    html=TEMPLATES / "inventory.report.html.j2",
    text=TEMPLATES / "inventory.report.txt.j2",
    description="Summary of a closed stock-take with the three reconciliation CSV reports.",
    trigger="Inventory closed",
    recipients="Team mailbox",
    entity_type="inventory",
    sample_context=INVENTORY_SAMPLE,
    build_context=inventory_context,
)
