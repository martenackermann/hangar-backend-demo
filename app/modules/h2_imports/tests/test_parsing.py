"""Reader, type detection, packed fields and column validation (SPEC §7.1, §7.2, §7.4.1)."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime

import pytest
import xlsxwriter

from app.core.enums import ImportType, ProcurementType
from app.modules.h2_imports.importing.formats import ORDER_DETAIL_KEYS, detect_type
from app.modules.h2_imports.importing.issues import FileProblem
from app.modules.h2_imports.importing.packed import parse_packed
from app.modules.h2_imports.importing.reader import detect_delimiter, normalize_header, read_table
from app.modules.h2_imports.importing.validate import validate_table
from app.modules.h2_imports.importing.values import (
    parse_date,
    parse_datetime,
    parse_email,
    parse_mac,
    parse_zip,
)

from .conftest import SR_HEADER, address, csv_bytes, order_details, sr_row


def codes(validation, number: int) -> list[str]:
    row = next(r for r in validation.rows if r.number == number)
    return [i.code for i in row.issues]


# --- reader --------------------------------------------------------------------------------


def test_header_normalization_and_delimiter() -> None:
    assert normalize_header("﻿ Retirement-Date ") == "retirement_date"
    assert normalize_header("Serial Number") == "serial_number"
    assert detect_delimiter("a;b;c") == ";"
    assert detect_delimiter("a,b;c") == ","
    assert detect_delimiter('"x;y",b') == ","


def test_csv_semicolon_bom_crlf_and_empty_rows() -> None:
    data = "﻿User ID;Last Name;City\r\nE100001;Hale;Riverton\r\n;;\r\nE100002;Brooks;\r\n"
    table = read_table(data.encode(), ".csv")
    assert table.delimiter == ";"
    assert table.normalized == ["user_id", "last_name", "city"]
    assert table.row_count == 2
    assert table.frame["__row"].to_list() == [2, 4]


@pytest.mark.parametrize(
    ("content", "code", "status"),
    [
        ("user_id,last_name\nE1,\xe4\n".encode("latin-1"), "encoding_unsupported", 400),
        (b"   \n", "empty_file", 400),
        (b"user_id,last_name\n", "empty_file", 400),
        (b"user_id,User ID,last_name\nA,B,C\n", "duplicate_columns", 400),
    ],
)
def test_file_problems(content: bytes, code: str, status: int) -> None:
    with pytest.raises(FileProblem) as caught:
        read_table(content, ".csv")
    assert (caught.value.code, caught.value.status) == (code, status)


def test_too_many_rows() -> None:
    content = b"user_id,last_name\n" + b"E100001,Hale\n" * 5
    with pytest.raises(FileProblem) as caught:
        read_table(content, ".csv", max_rows=4)
    assert (caught.value.code, caught.value.status) == ("too_many_rows", 413)


def test_xlsx_reads_text_and_iso_dates() -> None:
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer)
    sheet = workbook.add_worksheet()
    date_format = workbook.add_format({"num_format": "yyyy-mm-dd"})
    sheet.write_row(0, 0, ["asset_tag", "retirement_date", "user_id", "last_name", "count"])
    sheet.write(1, 0, "gx123456")
    sheet.write_datetime(1, 1, datetime(2026, 12, 1), date_format)  # noqa: DTZ001
    sheet.write(1, 2, "E100001")
    sheet.write(1, 3, "Hale")
    sheet.write(1, 4, 3)
    workbook.close()
    table = read_table(buffer.getvalue(), ".xlsx")
    assert table.delimiter is None
    validation = validate_table(table, ImportType.LEASE_RETURNS)
    row = validation.rows[0]
    assert row.issues == []
    assert row.values["asset_tag"] == "GX123456"
    assert row.values["retirement_date"] == date(2026, 12, 1)


def test_unreadable_xlsx() -> None:
    with pytest.raises(FileProblem) as caught:
        read_table(b"PK\x03\x04garbage", ".xlsx")
    assert caught.value.code == "file_unreadable"


# --- detection -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (["sr_number", "order_details", "retirement_date"], ImportType.SERVICE_REQUESTS),
        (["asset_tag", "retirement_date", "serial_number"], ImportType.LEASE_RETURNS),
        (["serial_number", "install_state"], ImportType.ASSET_STATUS_SYNC),
        (["serial_number", "model"], ImportType.ASSETS_NEW),
        (["user_id", "last_name"], ImportType.CONTACTS),
        (["user_id", "email"], None),
    ],
)
def test_detection_precedence(headers: list[str], expected: ImportType | None) -> None:
    assert detect_type(headers) == expected


# --- values --------------------------------------------------------------------------------


def test_value_parsers() -> None:
    assert parse_date("2026-02-28") == date(2026, 2, 28)
    with pytest.raises(ValueError, match="calendar"):
        parse_date("2026-02-30")
    assert parse_datetime("2026-10-12T08:14:00Z") == datetime(2026, 10, 12, 8, 14, tzinfo=UTC)
    assert parse_datetime("2026-10-12 08:14+02:00") == datetime(2026, 10, 12, 6, 14, tzinfo=UTC)
    # naive = business time (Europe/Zurich, CEST = UTC+2 in October)
    assert parse_datetime("2026-10-12T08:14") == datetime(2026, 10, 12, 6, 14, tzinfo=UTC)
    with pytest.raises(ValueError, match="date-time"):
        parse_datetime("12.10.2026")
    assert parse_email(" Mia.Turner@Example.com ") == "mia.turner@example.com"
    with pytest.raises(ValueError, match="email"):
        parse_email("mia@")
    assert parse_zip("5011") == "05011"
    with pytest.raises(ValueError, match="zip"):
        parse_zip("501100")
    assert parse_mac("02-00-5e-10-20-30") == "02:00:5E:10:20:30"
    with pytest.raises(ValueError, match="MAC"):
        parse_mac("XYZ")


# --- packed fields -------------------------------------------------------------------------


def test_packed_example_from_spec() -> None:
    value = (
        "[Procurement type: Replacement],[Scope of delivery: Laptop, dock, monitor],"
        "[Client component: Standard laptop],[Old base device: GX101877],[Old peripheral 1: N/A],"
        "[Old peripheral 2: N/A],[New base device type: Laptop Standard 14],"
        "[New peripheral 1 type: Docking Station USB-C],[New peripheral 2 type: Monitor 27],"
        "[Software without approval: Office Suite; PDF Reader],[Note: Please call: 9:00],"
        "[On-site contact phone: +00 555 0142]"
    )
    result = parse_packed(value, "order_details", ORDER_DETAIL_KEYS)
    assert result.issues == []
    assert result.values["scope_of_delivery"] == "Laptop, dock, monitor"
    assert result.values["old_peripheral_1"] is None
    assert result.values["note"] == "Please call: 9:00"


@pytest.mark.parametrize(
    ("value", "code", "severity"),
    [
        ("Procurement type: Replacement", "packed_format", "error"),
        ("[Procurement type Replacement]", "packed_format", "error"),
        (
            "[procurement TYPE: Removal],[Procurement type: Removal]",
            "packed_duplicate_key",
            "error",
        ),
        ("[Scope of delivery: x]", "packed_missing_key", "error"),
        ("[Procurement type: N/A]", "packed_missing_key", "error"),
        ("[Procurement type: Removal],[Color: Blue]", "packed_unknown_key", "warning"),
    ],
)
def test_packed_problems(value: str, code: str, severity: str) -> None:
    result = parse_packed(value, "order_details", ORDER_DETAIL_KEYS)
    assert [(i.code, i.severity) for i in result.issues] == [(code, severity)]


# --- validation ----------------------------------------------------------------------------


def _validate_srs(rows: list[dict[str, str]]):
    return validate_table(
        read_table(csv_bytes(SR_HEADER, rows), ".csv"), ImportType.SERVICE_REQUESTS
    )


def test_service_request_row_validation() -> None:
    rows = [
        sr_row("SR0099001"),
        sr_row("sr12ab", task_number=""),
        sr_row("SR0099003", delivery_address=address().replace("[Zip: 10110],", "")),
        sr_row("SR0099004", order_details=order_details() + ",[Procurement type: Removal]"),
        sr_row("SR0099005", order_details=order_details(new_base=None)),
        sr_row("SR0099006", order_details=order_details("Removal", new_base=None)),
        sr_row("SR0099007", opened_at="12/10/2026", email="not-an-email", gender="x"),
        sr_row("SR0099008", order_details=order_details("Upgrade")),
        sr_row("SR0099009", itsm_state="Pending", last_name=""),
        sr_row("SR0099001"),
        sr_row("SR0099011", cost_center="C" * 21),
    ]
    validation = _validate_srs(rows)
    assert codes(validation, 2) == []
    assert codes(validation, 3) == ["invalid_format"]
    assert codes(validation, 4) == ["packed_missing_key"]
    assert codes(validation, 5) == ["packed_duplicate_key"]
    assert codes(validation, 6) == ["packed_missing_key"]
    assert codes(validation, 7) == []
    assert sorted(codes(validation, 8)) == ["invalid_date", "invalid_email", "invalid_enum"]
    assert codes(validation, 9) == ["invalid_enum"]
    assert sorted(codes(validation, 10)) == ["invalid_enum", "missing_required"]
    assert codes(validation, 11) == ["duplicate_in_file"]
    assert codes(validation, 12) == ["too_long"]
    first = validation.rows[0]
    assert first.values["order_details"]["procurement_type"] == ProcurementType.REPLACEMENT
    assert first.values["delivery_address"]["zip"] == "10110"
    gender_issue = next(i for i in validation.rows[6].issues if i.column == "gender")
    assert gender_issue.severity == "warning"


def test_placeholders_are_null_and_keys_upper_cased() -> None:
    rows = [sr_row("SR0099001", task_number="n/a", cost_center="cc-4410", email="NULL")]
    row = _validate_srs(rows).rows[0]
    assert row.values["task_number"] is None
    assert row.values["cost_center"] == "CC-4410"
    assert row.values["email"] is None


def test_extra_columns_are_job_warnings() -> None:
    content = csv_bytes([*SR_HEADER, "color"], [sr_row("SR0099001", color="Blue")])
    validation = validate_table(read_table(content, ".csv"), ImportType.SERVICE_REQUESTS)
    assert validation.extra_columns == ["color"]
    assert validation.job_warnings[0]["code"] == "extra_columns"
