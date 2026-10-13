"""Notification template registry (SPEC §2.4, SPEC_RESOLUTIONS #13).

Templates live in the owning slice (``templates/<id>.html.j2`` / ``.txt.j2``); paths are
relative to the ``app/`` package (``"modules/h1_requests/templates/sr.invitation.html.j2"``)
or absolute. ``subject`` is a Jinja string. ``build_context(session, entity_id)`` renders a
preview with a real entity; ``sample_context`` is used without one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

APP_ROOT = Path(__file__).resolve().parents[2]

type Owner = Literal["core", "h1", "h2", "h3", "h4", "h5"]
type ContextBuilder = Callable[[AsyncSession, int], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class NotificationTemplate:
    id: str
    owner: Owner
    subject: str
    html: str | Path
    text: str | Path
    description: str
    name: str = ""
    trigger: str = ""
    recipients: str = ""
    entity_type: str | None = None
    sample_context: dict[str, Any] = field(default_factory=dict)
    build_context: ContextBuilder | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.id

    def template_path(self, kind: Literal["html", "text"]) -> str:
        """Loader name relative to ``app/`` (absolute paths are converted)."""
        raw = Path(self.html if kind == "html" else self.text)
        if raw.is_absolute():
            raw = raw.resolve().relative_to(APP_ROOT)
        return raw.as_posix()


class TemplateRegistry:
    def __init__(self) -> None:
        self._templates: dict[str, NotificationTemplate] = {}

    def register(self, template: NotificationTemplate) -> None:
        kinds: tuple[Literal["html", "text"], ...] = ("html", "text")
        for kind in kinds:
            path = APP_ROOT / template.template_path(kind)
            if not path.is_file():
                raise FileNotFoundError(f"Template {template.id}: {path} does not exist")
        self._templates[template.id] = template

    def get(self, template_id: str) -> NotificationTemplate:
        try:
            return self._templates[template_id]
        except KeyError as exc:
            raise KeyError(f"Unknown notification template {template_id!r}") from exc

    def __contains__(self, template_id: object) -> bool:
        return template_id in self._templates

    def all(self) -> list[NotificationTemplate]:
        return sorted(self._templates.values(), key=lambda t: t.id)

    def unregister(self, template_id: str) -> None:
        self._templates.pop(template_id, None)


templates = TemplateRegistry()
