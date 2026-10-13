"""Jinja2 rendering of notification templates inside the core base layout (SPEC §8.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from app.core.config import get_settings
from app.core.notifications.registry import APP_ROOT, NotificationTemplate, templates
from app.core.rules import salutation

BASE_HTML = "core/notifications/templates/base.html.j2"
BASE_TEXT = "core/notifications/templates/base.txt.j2"
OPERATOR = "Acme IT Services"
CLIENT = "Globex Fleet AG"


@dataclass(frozen=True, slots=True)
class RenderedMail:
    subject: str
    html: str
    text: str


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)  # noqa: DTZ001 - calendar date
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def format_date(value: Any) -> str:
    """``13 Oct 2026`` (business calendar date)."""
    dt = _as_datetime(value)
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(get_settings().tz)
    return f"{dt.day} {dt:%b %Y}"


def format_datetime(value: Any) -> str:
    """``13 Oct 2026, 09:30`` in the business time zone."""
    dt = _as_datetime(value)
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(get_settings().tz)
    return f"{dt.day} {dt:%b %Y, %H:%M}"


def format_time(value: Any) -> str:
    dt = _as_datetime(value)
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(get_settings().tz)
    return f"{dt:%H:%M}"


@lru_cache
def environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(APP_ROOT)),
        autoescape=select_autoescape(
            enabled_extensions=("html", "html.j2"), default_for_string=False
        ),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["date"] = format_date
    env.filters["datetime"] = format_datetime
    env.filters["time"] = format_time
    env.globals["salutation"] = salutation
    return env


def common_context() -> dict[str, Any]:
    settings = get_settings()
    return {
        "app_name": "Hangar",
        "operator_name": OPERATOR,
        "client_name": CLIENT,
        "team_mailbox": settings.mail_team_mailbox,
        "frontend_url": settings.frontend_url,
        "demo_mode": settings.demo_mode,
    }


def render_template(template: NotificationTemplate, context: dict[str, Any]) -> RenderedMail:
    env = environment()
    ctx = {**common_context(), **context}
    subject = " ".join(env.from_string(template.subject).render(ctx).split())
    body_html = env.get_template(template.template_path("html")).render(ctx)
    body_text = env.get_template(template.template_path("text")).render(ctx)
    html = env.get_template(BASE_HTML).render(
        {**ctx, "subject": subject, "body": Markup(body_html)}  # noqa: S704 - autoescaped template output
    )
    text = env.get_template(BASE_TEXT).render(
        {**ctx, "subject": subject, "body": body_text.strip()}
    )
    return RenderedMail(subject, html, text)


def render(template_id: str, context: dict[str, Any]) -> RenderedMail:
    return render_template(templates.get(template_id), context)
