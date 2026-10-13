"""Row issues (SPEC §7.8) and file-level problems."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

type Severity = Literal["error", "warning"]

#: SPEC §7.8 — default severity per row code (``invalid_email``/``invalid_enum`` vary).
ROW_CODES: dict[str, Severity] = {
    "missing_required": "error",
    "invalid_format": "error",
    "invalid_date": "error",
    "invalid_email": "error",
    "invalid_enum": "error",
    "too_long": "error",
    "duplicate_in_file": "error",
    "packed_format": "error",
    "packed_duplicate_key": "error",
    "packed_missing_key": "error",
    "packed_unknown_key": "warning",
    "unknown_article": "warning",
    "unknown_model": "warning",
    "unknown_contact": "warning",
    "email_missing": "warning",
    "asset_in_use": "error",
    "asset_in_workflow": "error",
    "asset_deactivated": "error",
    "asset_tag_conflict": "error",
    "unmapped_state": "warning",
    "items_locked": "warning",
    "lease_already_linked": "warning",
    "sr_not_found": "warning",
    "region_unresolved": "warning",
}


def truncate(value: Any, length: int = 500) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= length else text[: length - 1] + "…"


@dataclass(slots=True)
class Issue:
    severity: Severity
    code: str
    message: str
    column: str | None = None
    raw_value: str | None = None

    @classmethod
    def error(cls, code: str, message: str, column: str | None = None, raw: Any = None) -> Issue:
        return cls("error", code, message, column, truncate(raw))

    @classmethod
    def warning(cls, code: str, message: str, column: str | None = None, raw: Any = None) -> Issue:
        return cls("warning", code, message, column, truncate(raw))


@dataclass(slots=True)
class Row:
    """One data row after validation: ``values`` are parsed, ``raw`` the cleaned strings."""

    number: int
    raw: dict[str, str | None]
    values: dict[str, Any] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    key: str | None = None

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == "warning" for i in self.issues)

    def error(self, code: str, message: str, column: str | None = None, raw: Any = None) -> None:
        self.issues.append(Issue.error(code, message, column, raw))

    def warn(self, code: str, message: str, column: str | None = None, raw: Any = None) -> None:
        self.issues.append(Issue.warning(code, message, column, raw))


class RowError(Exception):
    """Raised by appliers: the row is rolled back and recorded with this error."""

    def __init__(self, code: str, message: str, column: str | None = None, raw: Any = None):
        super().__init__(message)
        self.issue = Issue.error(code, message, column, raw)


class FileProblem(Exception):
    """File-level problem (SPEC §7.8 last line): the upload is rejected, no job created."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        errors: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.errors = errors
