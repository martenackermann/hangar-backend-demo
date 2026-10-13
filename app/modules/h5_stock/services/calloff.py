"""Call-off orders (SPEC §4.8, §6.8 + SPEC_RESOLUTIONS #18; API.md §8.2)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import can
from app.core.config import get_settings
from app.core.enums import CallOffOrderStatus, label
from app.core.errors import ConflictError, GuardFailed, TransitionNotAllowed, ValidationProblem
from app.core.files import signed_url, store_file
from app.core.models import ArticleSiteSetting, Site, StoredFile, User
from app.core.notifications import notify
from app.core.read_models.refs import article_ref, site_ref, user_ref
from app.core.schemas import DownloadLink, EntityRef
from app.core.stock_math import above_max, below_min, order_qty
from app.core.timeutil import business_today, now_utc

from ..models import CallOffOrder, CallOffOrderLine
from ..schemas import (
    CallOffLine,
    CallOffOrderCreate,
    CallOffOrderOut,
    CallOffOrderPatch,
    CallOffOrderSummary,
    LineWarning,
)
from .common import require_hub
from .documents import PDF_TYPE, XLSX_TYPE, DocLine, OrderDocument, order_pdf, order_xlsx

S = CallOffOrderStatus
TEMPLATE_ID = "calloff.order"
ACTIONS = ("send", "cancel")


def entity_ref(order: CallOffOrder) -> EntityRef:
    return EntityRef(type="calloff_order", id=order.id, label=order.number)


async def settings_for(
    session: AsyncSession, site_id: int, article_ids: Sequence[int] | None = None
) -> dict[int, ArticleSiteSetting]:
    stmt = select(ArticleSiteSetting).where(ArticleSiteSetting.site_id == site_id)
    if article_ids is not None:
        stmt = stmt.where(ArticleSiteSetting.article_id.in_(article_ids))
    return {s.article_id: s for s in (await session.scalars(stmt)).all()}


async def next_number(session: AsyncSession, site: Site, day: date) -> str:
    prefix = f"CO-{site.code}-{day:%Y%m%d}-"
    existing = (
        await session.scalars(
            select(CallOffOrder.number).where(CallOffOrder.number.like(f"{prefix}%"))
        )
    ).all()
    taken = {int(n.rsplit("-", 1)[1]) for n in existing if n.rsplit("-", 1)[1].isdigit()}
    seq = 1
    while seq in taken:
        seq += 1
    return f"{prefix}{seq:02d}"


def line_from_setting(setting: ArticleSiteSetting, quantity: int | None = None) -> CallOffOrderLine:
    suggested = order_qty(setting.demand_qty, setting.stock_qty, setting.buffer_qty)
    return CallOffOrderLine(
        article_id=setting.article_id,
        article=setting.article,
        demand_qty=setting.demand_qty,
        stock_qty=setting.stock_qty,
        buffer_qty=setting.buffer_qty,
        suggested_qty=suggested,
        quantity=suggested if quantity is None else quantity,
    )


def _validate_delivery_date(value: date, field: str = "deliveryDate") -> None:
    if value < business_today():
        raise ValidationProblem({field: ["The delivery date must not be in the past."]})


async def create_order(
    session: AsyncSession, body: CallOffOrderCreate, actor: User
) -> CallOffOrder:
    site = await require_hub(session, body.site_id)
    _validate_delivery_date(body.delivery_date)
    today = business_today()
    order = CallOffOrder(
        number=await next_number(session, site, today),
        site_id=site.id,
        site=site,
        status=S.DRAFT,
        delivery_date=body.delivery_date,
        note=(body.note or "").strip() or None,
        recipient_email=get_settings().mail_partner_orders,
        created_by=actor.id,
        creator=actor,
    )
    settings = await settings_for(session, site.id)
    for setting in sorted(settings.values(), key=lambda s: s.article.number):
        if not setting.article.is_active:
            continue
        line = line_from_setting(setting)
        if line.suggested_qty > 0:
            order.lines.append(line)
    session.add(order)
    await session.flush()
    return order


def _require_draft(order: CallOffOrder, action: str) -> None:
    if order.status != S.DRAFT:
        raise TransitionNotAllowed(
            f"Action '{action}' is not allowed while the order is '{order.status}'.",
            action=action,
            current_status=order.status,
            allowed_actions=[],
        )


async def patch_order(
    session: AsyncSession, order: CallOffOrder, body: CallOffOrderPatch, actor: User
) -> CallOffOrder:
    if order.status != S.DRAFT:
        raise ConflictError("Only draft call-off orders can be edited.", code="conflict")
    fields = body.model_fields_set
    if "delivery_date" in fields:
        if body.delivery_date is None:
            raise ValidationProblem({"deliveryDate": ["A delivery date is required."]})
        _validate_delivery_date(body.delivery_date)
        order.delivery_date = body.delivery_date
    if "note" in fields:
        order.note = (body.note or "").strip() or None
    if "recipient_email" in fields:
        if not body.recipient_email:
            raise ValidationProblem({"recipientEmail": ["A recipient is required."]})
        order.recipient_email = str(body.recipient_email).lower()
    if body.lines:
        await _apply_lines(session, order, body)
    order.updated_at = now_utc()  # bump version even when only lines changed
    await session.flush()
    return order


async def _apply_lines(session: AsyncSession, order: CallOffOrder, body: CallOffOrderPatch) -> None:
    assert body.lines is not None
    article_ids = [line.article_id for line in body.lines]
    seen: set[int] = set()
    errors: dict[str, list[str]] = {}
    for index, article_id in enumerate(article_ids):
        if article_id in seen:
            errors[f"lines.{index}.articleId"] = ["This article is listed twice."]
        seen.add(article_id)
    settings = await settings_for(session, order.site_id, article_ids)
    by_article = {line.article_id: line for line in order.lines}
    for index, patch in enumerate(body.lines):
        if patch.article_id in by_article:
            continue
        setting = settings.get(patch.article_id)
        if setting is None:
            errors[f"lines.{index}.articleId"] = ["Unknown article."]
        elif not setting.article.is_active and patch.quantity > 0:
            errors[f"lines.{index}.articleId"] = ["This article is inactive."]
    if errors:
        raise ValidationProblem(errors)
    for patch in body.lines:
        line = by_article.get(patch.article_id)
        if line is None:
            if patch.quantity == 0:
                continue
            order.lines.append(line_from_setting(settings[patch.article_id], patch.quantity))
        elif patch.quantity == 0:
            order.lines.remove(line)
        else:
            line.quantity = patch.quantity


def _send_failure(order: CallOffOrder) -> tuple[str, str] | None:
    if not any(line.quantity > 0 for line in order.lines):
        return ("no_lines", "Add at least one line with a quantity above zero.")
    if order.delivery_date < business_today():
        return ("delivery_date_past", "The delivery date is in the past. Choose a new date.")
    return None


async def document_for(session: AsyncSession, order: CallOffOrder) -> OrderDocument:
    site = order.site
    address = ", ".join(
        p for p in (site.street, f"{site.zip or ''} {site.city or ''}".strip()) if p
    )
    lines = [
        DocLine(
            article_number=line.article.number,
            external_part_number=line.article.external_part_number,
            name=line.article.name,
            model_number=line.article.model_number,
            quantity=line.quantity,
            demand=line.demand_qty,
            stock=line.stock_qty,
            buffer=line.buffer_qty,
        )
        for line in sorted(order.lines, key=lambda line: line.article.number)
        if line.quantity > 0
    ]
    return OrderDocument(
        number=order.number,
        status_label=label(order.status),
        site_name=site.name,
        site_address=address or site.name,
        delivery_date=order.delivery_date,
        recipient_email=order.recipient_email,
        note=order.note,
        created_by=order.creator.display_name,
        created_at=order.created_at,
        sent_at=order.sent_at,
        lines=lines,
    )


async def _store_documents(
    session: AsyncSession, order: CallOffOrder, actor: User | None, *, temporary: bool
) -> tuple[StoredFile, StoredFile]:
    doc = await document_for(session, order)
    ttl = timedelta(hours=24) if temporary else None
    xlsx = await store_file(
        session,
        order_xlsx(doc),
        f"{order.number}.xlsx",
        XLSX_TYPE,
        "calloff_document",
        entity=entity_ref(order),
        ttl=ttl,
        created_by=actor,
    )
    pdf = await store_file(
        session,
        order_pdf(doc),
        f"{order.number}.pdf",
        PDF_TYPE,
        "calloff_document",
        entity=entity_ref(order),
        ttl=ttl,
        created_by=actor,
    )
    return xlsx, pdf


def mail_context(order: CallOffOrder, doc: OrderDocument) -> dict[str, Any]:
    return {
        "order_number": order.number,
        "site_name": doc.site_name,
        "site_address": doc.site_address,
        "delivery_date": order.delivery_date.isoformat(),
        "note": order.note or "",
        "sender_name": order.sender.display_name if order.sender else doc.created_by,
        "total_quantity": doc.total_quantity,
        "lines": [
            {
                "article_number": line.article_number,
                "external_part_number": line.external_part_number or "",
                "name": line.name,
                "model_number": line.model_number,
                "quantity": line.quantity,
            }
            for line in doc.lines
        ],
    }


async def send_order(session: AsyncSession, order: CallOffOrder, actor: User) -> CallOffOrder:
    _require_draft(order, "send")
    failure = _send_failure(order)
    if failure is not None:
        raise GuardFailed(*failure)
    now = now_utc()
    today = business_today(now)
    order.status = S.SENT
    order.sent_at = now
    order.sent_by = actor.id
    order.sender = actor
    xlsx, pdf = await _store_documents(session, order, actor, temporary=False)
    order.xlsx_file_id, order.pdf_file_id = xlsx.id, pdf.id
    settings = await settings_for(session, order.site_id, [line.article_id for line in order.lines])
    for line in order.lines:
        setting = settings.get(line.article_id)
        if setting is not None and line.quantity > 0:
            setting.last_order_date = today
            setting.last_ordered_qty = line.quantity
    await session.flush()
    doc = await document_for(session, order)
    await notify(
        session,
        TEMPLATE_ID,
        to=[order.recipient_email],
        cc=[get_settings().mail_team_mailbox],
        context=mail_context(order, doc),
        entity=entity_ref(order),
        attachments=[xlsx.id, pdf.id],
        actor=actor,
    )
    return order


async def cancel_order(
    session: AsyncSession, order: CallOffOrder, comment: str | None, actor: User
) -> CallOffOrder:
    _require_draft(order, "cancel")
    order.status = S.CANCELLED
    if comment and comment.strip():
        cancelled = f"Cancelled by {actor.display_name}: {comment.strip()}"
        order.note = f"{order.note}\n{cancelled}" if order.note else cancelled
    await session.flush()
    return order


async def document_link(
    session: AsyncSession, order: CallOffOrder, fmt: str, actor: User
) -> DownloadLink:
    """Sent orders: the stored file; drafts (or missing files): generated on the fly."""
    file_id = order.xlsx_file_id if fmt == "xlsx" else order.pdf_file_id
    if order.status == S.SENT and file_id is not None:
        stored = await session.get(StoredFile, file_id)
        if stored is not None:
            return signed_url(stored)
    xlsx, pdf = await _store_documents(session, order, actor, temporary=order.status != S.SENT)
    if order.status == S.SENT:
        order.xlsx_file_id, order.pdf_file_id = xlsx.id, pdf.id
    return signed_url(xlsx if fmt == "xlsx" else pdf)


# --- read model --------------------------------------------------------------------------------


def _line_warnings(line: CallOffOrderLine, setting: ArticleSiteSetting | None) -> list[LineWarning]:
    if setting is None:
        return []
    warnings: list[LineWarning] = []
    if below_min(line.stock_qty, line.quantity, setting.min_qty):
        warnings.append("below_min")
    if above_max(line.stock_qty, line.quantity, setting.max_qty):
        warnings.append("above_max")
    return warnings


def build_summary(order: CallOffOrder) -> CallOffOrderSummary:
    site = site_ref(order.site)
    creator = user_ref(order.creator)
    assert site is not None
    assert creator is not None
    return CallOffOrderSummary(
        id=order.id,
        number=order.number,
        site=site,
        status=order.status,
        delivery_date=order.delivery_date,
        line_count=sum(1 for line in order.lines if line.quantity > 0),
        total_quantity=sum(line.quantity for line in order.lines),
        recipient_email=order.recipient_email,
        created_by=creator,
        created_at=order.created_at,
        sent_at=order.sent_at,
        sent_by=user_ref(order.sender),
    )


async def build_order(
    session: AsyncSession, order: CallOffOrder, user: User | None
) -> CallOffOrderOut:
    settings = await settings_for(session, order.site_id)
    lines: list[CallOffLine] = []
    all_warnings: list[str] = []
    for line in sorted(order.lines, key=lambda item: item.article.number):
        setting = settings.get(line.article_id)
        warnings = _line_warnings(line, setting)
        for warning in warnings:
            if warning not in all_warnings:
                all_warnings.append(warning)
        article = article_ref(line.article)
        assert article is not None
        lines.append(
            CallOffLine(
                id=line.id,
                article=article,
                demand_qty=line.demand_qty,
                stock_qty=line.stock_qty,
                buffer_qty=line.buffer_qty,
                suggested_qty=line.suggested_qty,
                quantity=line.quantity,
                min_qty=setting.min_qty if setting else None,
                max_qty=setting.max_qty if setting else None,
                warnings=warnings,
            )
        )
    if not any(line.quantity > 0 for line in order.lines):
        all_warnings.append("zero_lines")
    if order.status == S.DRAFT and order.delivery_date < business_today():
        all_warnings.append("delivery_date_past")
    available: list[str] = []
    if order.status == S.DRAFT and user is not None and can(user, "calloff.manage"):
        if _send_failure(order) is None:
            available.append("send")
        available.append("cancel")
    summary = build_summary(order)
    return CallOffOrderOut(
        **summary.model_dump(by_alias=False),
        note=order.note,
        lines=lines,
        warnings=all_warnings,
        available_actions=available,
        version=order.version,
        mail_log_entity_ref=entity_ref(order),
    )
