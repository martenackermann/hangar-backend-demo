"""Failed-row report (SPEC §7.1 "Error report", §6.20 "Import errors").

CSV, UTF-8 with BOM, comma, RFC 4180 quoting: the original columns of the row followed by
``row_number, severity, column, code, message`` — one line per issue.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping, Sequence

from .issues import Issue

REPORT_COLUMNS = ("row_number", "severity", "column", "code", "message")


def build_error_report(
    headers: Sequence[str],
    original: Mapping[int, Sequence[str | None]],
    issues: Iterable[tuple[int, Issue]],
) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([*headers, *REPORT_COLUMNS])
    for row_number, issue in sorted(issues, key=lambda pair: pair[0]):
        cells = list(original.get(row_number, ()))
        cells += [None] * (len(headers) - len(cells))
        writer.writerow(
            [
                *("" if c is None else c for c in cells[: len(headers)]),
                row_number,
                issue.severity,
                issue.column or "",
                issue.code,
                issue.message,
            ]
        )
    return ("﻿" + buffer.getvalue()).encode("utf-8")
