"""Notifications: template registry, Jinja2 rendering with the base layout, ``notify()``."""

from app.core.notifications.registry import NotificationTemplate, TemplateRegistry, templates
from app.core.notifications.renderer import RenderedMail, render, render_template
from app.core.notifications.service import TASK_NAME, notify, send_notification

__all__ = [
    "TASK_NAME",
    "NotificationTemplate",
    "RenderedMail",
    "TemplateRegistry",
    "notify",
    "render",
    "render_template",
    "send_notification",
    "templates",
]
