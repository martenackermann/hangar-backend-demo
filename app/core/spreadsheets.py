"""Spreadsheet export hygiene: no formula injection through user-controlled text.

Imported data (names, e-mail addresses, notes, raw import rows) ends up in CSV and XLSX
exports. A cell starting with ``=``, ``+``, ``-``, ``@``, tab or CR may be evaluated as a
formula when the file is opened in a spreadsheet program (CSV/DDE injection). CSV cells are
neutralised with a leading apostrophe; XLSX workbooks are opened with
:data:`XLSX_WORKBOOK_OPTIONS` so text is always written as text.
"""

from __future__ import annotations

import re
from typing import Any

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
#: Signed numbers and phone-like values ("+41 44 123 45 67", "-12", "-1.5") cannot call
#: functions; they stay untouched so numeric columns and phone numbers survive a round trip.
_PLAIN_SIGNED = re.compile(r"^[+-][0-9 ().,/-]*$")

#: ``xlsxwriter.Workbook(buffer, {**XLSX_WORKBOOK_OPTIONS, ...})``: never turn strings into
#: formulas, hyperlinks or numbers.
XLSX_WORKBOOK_OPTIONS: dict[str, Any] = {
    "strings_to_formulas": False,
    "strings_to_urls": False,
    "strings_to_numbers": False,
}


def safe_cell(value: str) -> str:
    """``value`` for a CSV cell, prefixed with ``'`` when it could start a formula."""
    if value.startswith(_FORMULA_START) and not _PLAIN_SIGNED.match(value):
        return "'" + value
    return value


def safe_csv_value(value: Any) -> Any:
    """:func:`safe_cell` for strings; numbers, dates and ``None`` pass through unchanged."""
    return safe_cell(value) if isinstance(value, str) else value
