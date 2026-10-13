"""Validation (SPEC §7.1): polars cleans and checks column-wise, then cells are parsed per row.

Column-wise (vectorised, polars): trim + placeholder → null, business keys upper-cased,
``missing_required``, ``too_long``, ``invalid_format`` (regex) and ``duplicate_in_file``.
Per row (Python): dates, emails, enums, zip, MAC, phone and the packed columns.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from app.core.enums import ImportType, ProcurementType

from . import values as cell
from .formats import SPECS
from .issues import Issue, Row
from .packed import parse_packed
from .reader import ROW_COL, RawTable
from .spec import Column, Kind, PackedKey, TypeSpec

_NULL_UPPER = list(cell.NULL_TOKENS)


@dataclass(slots=True)
class Validation:
    type: ImportType
    rows: list[Row]
    extra_columns: list[str]
    missing_columns: list[str]
    #: original cells per row number, in file column order (for the error report)
    original: dict[int, list[str | None]] = field(default_factory=dict)
    original_headers: list[str] = field(default_factory=list)

    @property
    def job_warnings(self) -> list[dict[str, str]]:
        if not self.extra_columns:
            return []
        return [
            {
                "code": "extra_columns",
                "message": f"Ignored columns: {', '.join(self.extra_columns)}.",
            }
        ]


def header_analysis(normalized: list[str], spec: TypeSpec) -> tuple[list[str], list[str]]:
    """(missing required columns, extra columns) for ``spec``."""
    present = [h for h in normalized if h]
    known = set(spec.column_names)
    missing = [c for c in spec.required_columns if c not in present]
    extra = [h for h in present if h not in known]
    return missing, extra


def _clean_expr(column: Column) -> pl.Expr:
    trimmed = pl.col(column.name).str.strip_chars()
    cleaned = (
        pl.when(trimmed.is_null() | trimmed.str.to_uppercase().is_in(_NULL_UPPER))
        .then(None)
        .otherwise(trimmed)
    )
    if column.kind == Kind.KEY:
        cleaned = cleaned.str.to_uppercase()
    return cleaned.alias(column.name)


def _column_issues(frame: pl.DataFrame, spec: TypeSpec) -> dict[int, list[Issue]]:
    """Vectorised checks; returns issues per row number."""
    issues: dict[int, list[Issue]] = {}

    def add(mask: pl.Expr, column: Column, factory: Callable[[str | None], Issue]) -> None:
        hits = frame.filter(mask).select(ROW_COL, column.name)
        for row_number, value in hits.iter_rows():
            issues.setdefault(int(row_number), []).append(factory(value))

    for column in spec.columns:
        col = pl.col(column.name)
        if column.required:
            add(
                col.is_null(),
                column,
                lambda _v, c=column: Issue.error(
                    "missing_required", f"'{c.name}' is required.", c.name
                ),
            )
        if column.max_len is not None:
            add(
                col.is_not_null() & (col.str.len_chars() > column.max_len),
                column,
                lambda v, c=column: Issue.error(
                    "too_long", f"'{c.name}' is longer than {c.max_len} characters.", c.name, v
                ),
            )
        if column.pattern is not None:
            too_long = (
                col.str.len_chars() > column.max_len
                if column.max_len is not None
                else pl.lit(value=False)
            )
            add(
                col.is_not_null() & ~too_long & ~col.str.contains(column.pattern),
                column,
                lambda v, c=column: Issue.error(
                    "invalid_format",
                    f"'{v}' does not match the format of '{c.name}' ({c.format}).",
                    c.name,
                    v,
                ),
            )
    key = pl.col(spec.key)
    duplicates = frame.filter(key.is_not_null() & ~key.is_first_distinct()).select(
        ROW_COL, spec.key
    )
    for row_number, value in duplicates.iter_rows():
        issues.setdefault(int(row_number), []).append(
            Issue.error(
                "duplicate_in_file",
                f"{value} appears more than once in this file; only the first row is imported.",
                spec.key,
                value,
            )
        )
    return issues


def _parse_cell(row: Row, kind: Kind, name: str, column: str, raw: str) -> Any:
    """Parse one non-null cell; records issues on ``row`` and returns the value (or None)."""
    try:
        match kind:
            case Kind.DATE:
                return cell.parse_date(raw)
            case Kind.DATETIME:
                return cell.parse_datetime(raw)
            case Kind.DATE_OR_DATETIME:
                return cell.parse_date_or_datetime(raw)
            case Kind.EMAIL | Kind.OPTIONAL_EMAIL:
                return cell.parse_email(raw)
            case Kind.GENDER:
                return cell.parse_gender(raw)
            case Kind.PROCUREMENT:
                return cell.parse_procurement(raw)
            case Kind.ZIP:
                return cell.parse_zip(raw)
            case Kind.MAC:
                return cell.parse_mac(raw)
            case Kind.PHONE:
                return cell.parse_phone(raw)
            case Kind.SOFTWARE:
                return cell.parse_software(raw)
            case Kind.ITSM_STATE:
                state, known = cell.parse_itsm_state(raw)
                if not known:
                    row.warn(
                        "invalid_enum",
                        f"Unknown ITSM state '{raw}' (stored as given).",
                        column,
                        raw,
                    )
                return state
            case _:
                return raw
    except ValueError as exc:
        message = str(exc)
        match kind:
            case Kind.DATE | Kind.DATETIME | Kind.DATE_OR_DATETIME:
                row.error("invalid_date", message, column, raw)
            case Kind.EMAIL:
                row.error("invalid_email", message, column, raw)
            case Kind.OPTIONAL_EMAIL:
                row.warn("invalid_email", f"{message} It was ignored.", column, raw)
            case Kind.GENDER:
                row.warn("invalid_enum", f"{message} It was ignored.", column, raw)
            case Kind.PROCUREMENT:
                row.error("invalid_enum", message, column, raw)
            case Kind.MAC | Kind.PHONE:
                row.warn("invalid_format", f"{message} It was ignored.", column, raw)
            case _:
                row.error("invalid_format", message, column, raw)
        return None


def _check_packed_value(row: Row, key: PackedKey, column: str, value: str) -> Any:
    label = f"{column}.{key.key}"
    if key.max_len is not None and len(value) > key.max_len:
        row.error("too_long", f"'{key.key}' is longer than {key.max_len} characters.", label, value)
        return None
    if key.kind == Kind.KEY:
        upper = value.upper()
        if key.pattern is not None and not re.match(key.pattern, upper):
            row.error("invalid_format", f"'{value}' is not a valid asset tag.", label, value)
            return None
        return upper
    return _parse_cell(row, key.kind, key.name, label, value)


def _parse_packed_column(row: Row, column: Column, keys: tuple[PackedKey, ...]) -> dict[str, Any]:
    raw = row.raw.get(column.name)
    result = parse_packed(raw, column.name, keys, check_required=raw is not None)
    row.issues.extend(result.issues)
    parsed: dict[str, Any] = {}
    for key in keys:
        value = result.values.get(key.name)
        parsed[key.name] = (
            None if value is None else _check_packed_value(row, key, column.name, value)
        )
    parsed["__present__"] = result.present
    return parsed


def _service_request_rules(row: Row) -> None:
    details = row.values.get("order_details")
    if not isinstance(details, dict):
        return
    procurement = details.get("procurement_type")
    if (
        procurement is not None
        and procurement != ProcurementType.REMOVAL
        and details.get("new_base") is None
        and not any(i.code.startswith("packed_") and i.severity == "error" for i in row.issues)
    ):
        row.error(
            "packed_missing_key",
            "Required key 'New base device type' is missing (only removals may omit it).",
            "order_details.New base device type",
        )


_TYPE_RULES: dict[ImportType, Callable[[Row], None]] = {
    ImportType.SERVICE_REQUESTS: _service_request_rules,
}


def validate_table(table: RawTable, import_type: ImportType) -> Validation:
    spec = SPECS[import_type]
    missing, extra = header_analysis(table.normalized, spec)
    frame = table.frame
    absent = [c.name for c in spec.columns if c.name not in frame.columns]
    if absent:
        frame = frame.with_columns([pl.lit(None, dtype=pl.String).alias(n) for n in absent])
    original_names = [n for n in table.frame.columns if n != ROW_COL]
    original_headers = [h for h, n in zip(table.headers, table.normalized, strict=True) if n]
    original = {
        int(r[0]): list(r[1:]) for r in table.frame.select(ROW_COL, *original_names).iter_rows()
    }
    frame = frame.select(ROW_COL, *[_clean_expr(c) for c in spec.columns])
    column_issues = _column_issues(frame, spec)

    rows: list[Row] = []
    rule = _TYPE_RULES.get(import_type)
    for record in frame.iter_rows(named=True):
        number = int(record[ROW_COL])
        raw = {c.name: record[c.name] for c in spec.columns}
        row = Row(number=number, raw=raw, key=raw.get(spec.key))
        row.issues.extend(column_issues.get(number, []))
        failed_columns = {i.column for i in row.issues if i.severity == "error"}
        for column in spec.columns:
            value = raw[column.name]
            if column.kind == Kind.PACKED:
                if column.name in failed_columns:
                    row.values[column.name] = None
                    continue
                row.values[column.name] = _parse_packed_column(
                    row, column, spec.packed[column.name]
                )
            elif value is None or column.name in failed_columns:
                row.values[column.name] = None
            elif column.kind in (Kind.TEXT, Kind.KEY):
                row.values[column.name] = value
            else:
                row.values[column.name] = _parse_cell(
                    row, column.kind, column.name, column.name, value
                )
        if rule is not None:
            rule(row)
        rows.append(row)
    return Validation(
        type=import_type,
        rows=rows,
        extra_columns=extra,
        missing_columns=missing,
        original=original,
        original_headers=original_headers,
    )
