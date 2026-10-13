"""Reading uploads into a polars frame (SPEC §7.1 "Encoding", "CSV dialect", "XLSX", "Header").

Every cell is read as text. The frame's columns are the *normalized* headers (blank header
cells are dropped); ``__row`` holds the spreadsheet row number (header = 1, first data
row = 2). Completely empty rows are dropped (their row numbers are not reused).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

import polars as pl

from .issues import FileProblem

ROW_COL = "__row"
_ISO_MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T]00:00:00(?:\.0+)?$")
_ISO_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\.\d+)?$")


@dataclass(slots=True)
class RawTable:
    headers: list[str]
    normalized: list[str]
    delimiter: str | None
    frame: pl.DataFrame  # columns: ROW_COL + unique normalized headers, all pl.String

    @property
    def row_count(self) -> int:
        return self.frame.height


def normalize_header(value: str | None) -> str:
    """Strip BOM, trim, lower-case, spaces/hyphens → ``_`` (SPEC §7.1 "Header")."""
    text = (value or "").replace("﻿", "").strip().lower()
    return re.sub(r"[\s\-]+", "_", text)


def detect_delimiter(header_line: str) -> str:
    """``;`` if it yields more fields than ``,``, else ``,`` (SPEC §7.1 "CSV dialect")."""

    def count(delimiter: str) -> int:
        try:
            return len(next(csv.reader([header_line], delimiter=delimiter, quotechar='"')))
        except (csv.Error, StopIteration):
            return 0

    return ";" if count(";") > count(",") else ","


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileProblem(
            "encoding_unsupported",
            "The file is not UTF-8 encoded. Save it as 'CSV UTF-8' and upload it again.",
        ) from exc
    return text.removeprefix("﻿")


def _read_csv(data: bytes) -> tuple[pl.DataFrame, str]:
    text = _decode(data)
    if not text.strip():
        raise FileProblem("empty_file", "The file is empty.")
    first_line = text.splitlines()[0] if text else ""
    delimiter = detect_delimiter(first_line)
    try:
        frame = pl.read_csv(
            io.BytesIO(text.encode("utf-8")),
            has_header=False,
            infer_schema=False,
            separator=delimiter,
            quote_char='"',
            truncate_ragged_lines=True,
            raise_if_empty=True,
        )
    except pl.exceptions.NoDataError as exc:
        raise FileProblem("empty_file", "The file is empty.") from exc
    except Exception as exc:
        raise FileProblem("file_unreadable", f"The file could not be read: {exc}") from exc
    return frame, delimiter


def _excel_cell(value: str | None) -> str | None:
    """Date cells arrive as ``YYYY-MM-DD HH:MM:SS``; midnight → ISO date (SPEC §7.1 "XLSX")."""
    if value is None:
        return None
    if match := _ISO_MIDNIGHT.match(value):
        return match.group(1)
    if match := _ISO_DATETIME.match(value):
        return f"{match.group(1)}T{match.group(2)}"
    return value


def _read_xlsx(data: bytes) -> pl.DataFrame:
    try:
        import fastexcel

        reader = fastexcel.read_excel(data)
        if not reader.sheet_names:
            raise FileProblem("empty_file", "The workbook has no worksheet.")
        sheet = reader.load_sheet(0, header_row=None, dtypes="string")
        frame = sheet.to_polars()
    except FileProblem:
        raise
    except Exception as exc:
        raise FileProblem("file_unreadable", f"The workbook could not be read: {exc}") from exc
    if frame.height == 0:
        raise FileProblem("empty_file", "The file is empty.")
    frame = frame.select(pl.all().cast(pl.String))
    return frame.with_columns(
        pl.all().map_elements(_excel_cell, return_dtype=pl.String, skip_nulls=True)
    )


def read_table(data: bytes, extension: str, *, max_rows: int | None = None) -> RawTable:
    """Parse an upload; raises :class:`FileProblem` for file-level problems."""
    if extension == ".xlsx":
        frame, delimiter = _read_xlsx(data), None
    else:
        frame, delimiter = _read_csv(data)
    raw_columns = frame.columns
    header_cells = [frame[c][0] for c in raw_columns]
    headers = [(h or "").replace("﻿", "").strip() for h in header_cells]
    normalized = [normalize_header(h) for h in headers]
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for name in normalized:
        if not name:
            continue
        seen[name] = seen.get(name, 0) + 1
        if seen[name] == 2:
            duplicates.append(name)
    if duplicates:
        raise FileProblem(
            "duplicate_columns",
            f"Duplicate columns: {', '.join(duplicates)}.",
            errors={"file": [f"Duplicate columns: {', '.join(duplicates)}."]},
        )
    keep = [(raw, name) for raw, name in zip(raw_columns, normalized, strict=True) if name]
    if not keep:
        raise FileProblem("file_unreadable", "The first row contains no column headers.")
    body = (
        frame.slice(1)
        .select([pl.col(raw).alias(name) for raw, name in keep])
        .with_row_index(ROW_COL, offset=2)
        .with_columns(pl.col(ROW_COL).cast(pl.Int64))
    )
    names = [name for _, name in keep]
    non_empty = pl.any_horizontal(
        [pl.col(n).is_not_null() & (pl.col(n).str.strip_chars() != "") for n in names]
    )
    body = body.filter(non_empty)
    if body.height == 0:
        raise FileProblem("empty_file", "The file contains a header row but no data rows.")
    if max_rows is not None and body.height > max_rows:
        raise FileProblem(
            "too_many_rows",
            f"The file has {body.height:,} data rows; at most {max_rows:,} are allowed.",
            status=413,
        )
    return RawTable(headers=headers, normalized=normalized, delimiter=delimiter, frame=body)
