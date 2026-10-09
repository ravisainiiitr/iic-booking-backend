"""Parse the SRIC Wallet_Recharge.csv file and the values in it."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

COLUMNS = {
    "project_number": ("project number", "project no", "project no.", "project"),
    "pi_name": ("pi name", "pi", "name"),
    "employee_id": ("employee id", "employee no", "employee no.", "emp id", "emp no", "employee"),
    "ledger_id": ("ledger id", "ledger no", "ledger", "ledger number"),
    "receiver_code": ("receiver project", "receiver", "receiver project number", "receiver type"),
    "amount": ("amount", "amount (rs)", "amount (inr)", "amount in rs"),
}
REQUIRED = ("employee_id", "ledger_id", "receiver_code", "amount")
ATTACHMENT_RE = re.compile(r"^wallet[_ ]recharge(\s*\(\d+\))?\.csv$", re.I)


@dataclass
class ParsedRow:
    row_number: int
    project_number: str = ""
    pi_name: str = ""
    employee_id: str = ""
    ledger_id: str = ""
    receiver_code: str = ""
    amount_raw: str = ""
    amount: Decimal | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class ParsedFile:
    rows: list[ParsedRow]
    error: str = ""


def is_wallet_recharge_attachment(filename: str, expected: str = "Wallet_Recharge.csv") -> bool:
    name = (filename or "").strip().strip('"').rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not name:
        return False
    stem = re.sub(r"\.csv$", "", (expected or "Wallet_Recharge.csv").strip(), flags=re.I)
    pattern = re.compile(rf"^{re.escape(stem)}(\s*\(\d+\))?\.csv$", re.I)
    return bool(pattern.match(name) or ATTACHMENT_RE.match(name))


def decode_bytes(content: bytes) -> str:
    if content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff"):
        return content.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "").replace("\ufeff", "")).strip().strip('"').strip()


def _header_key(cell: str) -> str | None:
    norm = re.sub(r"[^a-z0-9 .()]", " ", _clean(cell).lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    for key, names in COLUMNS.items():
        if norm in names:
            return key
    return None


def parse_amount(raw: str) -> Decimal | None:
    text = _clean(raw).replace("₹", "").replace(",", "").replace(" ", "")
    text = re.sub(r"^(rs\.?|inr)", "", text, flags=re.I)
    if not re.fullmatch(r"-?\d+(\.\d+)?", text or ""):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if value != value.quantize(Decimal("0.01")):
        return None
    return value.quantize(Decimal("0.01"))


def normalize_employee_id(value: str) -> str:
    text = re.sub(r"\s+", "", _clean(value)).upper()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    stripped = text.lstrip("0")
    return stripped or ("0" if text else "")


def normalize_ledger_id(value: str) -> str:
    return re.sub(r"\s+", "", _clean(value)).upper()


def normalize_receiver_code(value: str) -> str:
    return re.sub(r"\s+", "", _clean(value)).upper()


def financial_year(day: date | datetime) -> str:
    """Indian financial year (April–March) as '2026-27'."""
    if isinstance(day, datetime):
        day = day.date()
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def parse_wallet_recharge_csv(content: bytes | str) -> ParsedFile:
    text = decode_bytes(content) if isinstance(content, (bytes, bytearray)) else str(content or "")
    text = text.lstrip("\ufeff")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ParsedFile(rows=[], error="The file is empty.")
    sample = lines[0]
    delimiter = ";" if sample.count(";") > sample.count(",") else ("\t" if sample.count("\t") > sample.count(",") else ",")
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter, skipinitialspace=True)
    records = list(reader)
    header = records[0]
    index: dict[str, int] = {}
    for i, cell in enumerate(header):
        key = _header_key(cell)
        if key and key not in index:
            index[key] = i
    missing = [k for k in REQUIRED if k not in index]
    if missing:
        return ParsedFile(rows=[], error="Missing column(s): " + ", ".join(m.replace("_", " ") for m in missing))

    rows: list[ParsedRow] = []
    for number, record in enumerate(records[1:], start=1):
        if not any(_clean(c) for c in record):
            continue

        def cell(key: str) -> str:
            i = index.get(key)
            return _clean(record[i]) if i is not None and i < len(record) else ""

        row = ParsedRow(
            row_number=number,
            project_number=cell("project_number")[:120],
            pi_name=cell("pi_name")[:255],
            employee_id=cell("employee_id")[:60],
            ledger_id=normalize_ledger_id(cell("ledger_id"))[:80],
            receiver_code=normalize_receiver_code(cell("receiver_code"))[:80],
            amount_raw=cell("amount")[:60],
        )
        if len(record) > len(header) and delimiter == ",":
            # An amount written as 10,000 without quotes spills into extra columns.
            extra = [_clean(c) for c in record[len(header) - 1 :]]
            if index["amount"] == len(header) - 1 and all(re.fullmatch(r"\d{2,3}(\.\d+)?|\d+", c) for c in extra):
                row.amount_raw = ",".join(extra)[:60]
        row.amount = parse_amount(row.amount_raw)
        if not row.ledger_id:
            row.errors.append("missing_ledger_id")
        if not row.employee_id:
            row.errors.append("missing_employee_id")
        if not row.receiver_code:
            row.errors.append("missing_receiver")
        if row.amount is None or row.amount <= 0:
            row.errors.append("invalid_amount")
        rows.append(row)
    if not rows:
        return ParsedFile(rows=[], error="No data rows in the file.")
    return ParsedFile(rows=rows)
