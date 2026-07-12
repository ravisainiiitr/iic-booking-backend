"""
Parser for IIC wallet recharge text file (e.g. IIC Wallet-27-02-2026.txt).
Aligned with IICFundParser PHP logic: main row ^\\|\\d+\\s*\\|, continuation ^\\|\\s+\\|,
cleanAmount (strip commas), employee number as 6-digit only \\b(\\d{6})\\b.

Received From is free text; we extract employee number by:
  1) keyword 'EMP NO' (or 'EMP  NO') followed by digits, or
  2) first standalone 6-digit number in the text (PHP style).

Supports: (1) Pipe-delimited, (2) Tab/CSV with header row.
"""

import re
from csv import reader as csv_reader
from datetime import datetime
from decimal import Decimal
from io import StringIO
from typing import Any, Dict, List, Optional


# Expected header variants (normalized: lower, strip)
HEADER_SLNO = "slno"
HEADER_DATED = "dated"
HEADER_RECEIPT_NO = "receiptno"
HEADER_CREDITED_TO_PROJECT = "credited to project no."
HEADER_AMOUNT = "amount(rs)"
HEADER_PAYMENT_DETAILS = "payment details"
HEADER_RECEIVED_FROM = "received from"
HEADER_REMARKS = "remarks"

# Match 'EMP NO' (with one or more spaces) or 'EMPNO', then optional separators, then digits
RE_EMP_NO = re.compile(
    r"EMP\s+NO\s*[:\s\-.]*(\d+)|EMPNO\s*[:\s\-.]*(\d+)",
    re.IGNORECASE,
)
# PHP IICFundParser: extract 6-digit employee number ONLY (standalone word)
RE_EMP_NO_6DIGIT = re.compile(r"\b(\d{6})\b")
RE_DEPT = re.compile(r"DEPT[-\s]*([A-Za-z0-9\s\-]+?)(?:\s+EMP|\s*$)", re.IGNORECASE)
# Optional full pattern for name + emp_no + department: PROF-<Name> EMP NO-<num> DEPT-OF <dept>
RE_RECEIVED_FROM = re.compile(
    r"(PROF-[A-Za-z\s]+)\s+EMP\s+NO-(\d+)\s+DEPT-OF\s+(.+)",
    re.IGNORECASE,
)


def _normalize_header(h: str) -> str:
    return (h or "").strip().lower().replace(" ", "").replace(".", "").replace("-", "")


def _header_map(row: List[str]) -> Dict[str, int]:
    """Map normalized header name -> column index."""
    out = {}
    for i, cell in enumerate(row):
        normalized = _normalize_header(cell)
        if not normalized:
            continue
        # Allow partial match for common variants
        if "slno" in normalized or normalized == "slnumber" or normalized == "sno":
            out[HEADER_SLNO] = i
        elif "dated" in normalized or normalized == "date":
            out[HEADER_DATED] = i
        elif "receipt" in normalized and "no" in normalized:
            out[HEADER_RECEIPT_NO] = i
        elif normalized == "receiptno":
            out[HEADER_RECEIPT_NO] = i
        elif "credited" in normalized and "project" in normalized:
            out[HEADER_CREDITED_TO_PROJECT] = i
        elif "amount" in normalized and "rs" in normalized:
            out[HEADER_AMOUNT] = i
        elif "amount" in normalized and HEADER_AMOUNT not in out:
            out[HEADER_AMOUNT] = i
        # Payment details
        elif "payment" in normalized and "detail" in normalized:
            out[HEADER_PAYMENT_DETAILS] = i
        elif "payment" in normalized and HEADER_PAYMENT_DETAILS not in out:
            out[HEADER_PAYMENT_DETAILS] = i
        # Received From (or "Name" column holding payer string)
        elif "received" in normalized and "from" in normalized:
            out[HEADER_RECEIVED_FROM] = i
        elif (normalized == "name" or "creditedto" in normalized) and HEADER_RECEIVED_FROM not in out:
            out[HEADER_RECEIVED_FROM] = i
        elif "remark" in normalized:
            out[HEADER_REMARKS] = i
    return out


def _parse_dated(s: str) -> Optional[datetime]:
    """Parse 'Feb 27, 2026', '27-02-2026', etc. to date."""
    if not s or not str(s).strip():
        return None
    s = str(s).strip()
    formats = (
        "%b %d, %Y", "%B %d, %Y",   # Feb 27, 2026
        "%d-%b-%Y", "%d/%b/%Y",     # 27-Feb-2026
        "%d-%m-%Y", "%d/%m/%Y",     # 27-02-2026
        "%m-%d-%Y", "%m/%d/%Y",     # 02-27-2026
        "%Y-%m-%d", "%Y/%m/%d",     # 2026-02-27
    )
    for fmt in formats:
        try:
            return datetime.strptime(s[:50].strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_amount(s: str) -> Optional[Decimal]:
    """Parse amount: strip commas and return Decimal (PHP cleanAmount: floatval(str_replace(',', '', trim)))."""
    if s is None or str(s).strip() == "":
        return None
    s = str(s).strip().replace(",", "")
    try:
        return Decimal(s)
    except Exception:
        return None


def _extract_emp_no(received_from: str) -> Optional[str]:
    """
    Extract employee number from 'Received From' text (aligned with PHP IICFundParser).
    1) If 'EMP NO' (or 'EMP  NO') is present, use the number that follows it.
    2) Else use first standalone 6-digit number in the text (PHP: preg_match('/\\b(\\d{6})\\b/')).
    """
    if not received_from:
        return None
    normalized = " ".join(str(received_from).split()).replace("\xa0", " ")
    m = RE_EMP_NO.search(normalized)
    if m:
        return (m.group(1) or m.group(2) or "").strip()
    m6 = RE_EMP_NO_6DIGIT.search(normalized)
    if m6:
        return m6.group(1)
    return None


def _extract_dept_hint(received_from: str) -> Optional[str]:
    """Extract department hint from '... DEPT-OF HYDROLOGY ...' -> 'OF HYDROLOGY' or 'HYDROLOGY'."""
    if not received_from:
        return None
    m = RE_DEPT.search(received_from)
    if not m:
        return None
    hint = m.group(1).strip()
    # Normalize "OF HYDROLOGY" -> "HYDROLOGY" for matching
    if hint.upper().startswith("OF "):
        hint = hint[3:].strip()
    return hint or None


def _extract_name(received_from: str) -> str:
    """Extract name from 'PROF-BRIJESH KUMAR YADAV EMP NO-100584 DEPT-...' -> 'BRIJESH KUMAR YADAV'."""
    if not received_from or not received_from.strip():
        return ""
    s = received_from.strip()
    # Drop everything from " EMP NO" or " EMP NO-" onwards
    idx = re.search(r"\s+EMP\s*NO", s, re.IGNORECASE)
    if idx:
        s = s[: idx.start()].strip()
    # Optional: strip common prefixes (PROF-, DR-, etc.)
    for prefix in ("PROF-", "DR-", "PROF ", "DR "):
        if s.upper().startswith(prefix.upper()):
            s = s[len(prefix) :].strip()
            break
    return s or ""


def _parse_pipe_format(lines: List[str]) -> List[Dict[str, Any]]:
    """
    Parse pipe-delimited format (IICFundParser PHP style).
    Main row: ^\\|\\d+\\s*\\| (starts with |, digits, optional space, |).
    Continuation: ^\\|\\s+\\| (starts with |, whitespace, |); append column 7 to received_from.
    Data can span multiple continuation lines; flush when next main row or EOF.
    Columns: [2]=date, [3]=receipt_no, [4]=project_no, [5]=amount, [6]=payment, [7]=received_from.
    cleanAmount: strip commas then parse. Employee no: EMP NO + digits, or first 6-digit number.
    """
    records: List[Dict[str, Any]] = []
    current_record: Dict[str, Any] = {}

    def flush_record(rec: Dict[str, Any]) -> None:
        received_from = rec.get("received_from") or ""
        rec["emp_no"] = _extract_emp_no(received_from) or ""
        m = RE_RECEIVED_FROM.search(received_from)
        if m:
            rec["name"] = m.group(1).strip()
            dept = m.group(3).strip()
            if dept.upper().startswith("OF "):
                dept = dept[3:].strip()
            rec["department"] = dept
        else:
            rec["name"] = _extract_name(received_from)
            rec["department"] = _extract_dept_hint(received_from) or ""
        amount = _parse_amount(rec.get("amount_raw") or "")
        if amount is not None and amount > 0:
            dt = _parse_dated(rec.get("date") or "")
            records.append({
                "sl_no": "",
                "dated": dt.date() if dt else None,
                "receipt_no": rec.get("receipt_no") or "",
                "credited_to_project_no": rec.get("project_no") or "",
                "amount": amount,
                "payment_details": rec.get("payment") or "",
                "received_from": received_from,
                "remarks": "",
                "emp_no": rec.get("emp_no") or None,
                "dept_hint": rec.get("department") or None,
                "name": rec.get("name") or "",
            })

    for line in lines:
        line = line.rstrip("\n\r")
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 8:
            continue

        # Main data row (PHP: preg_match('/^\|\d+\s*\|/', $line))
        if re.match(r"^\|\d+\s*\|", line):
            if current_record:
                flush_record(current_record)
            current_record = {
                "date": parts[2].strip() if len(parts) > 2 else "",
                "receipt_no": parts[3].strip() if len(parts) > 3 else "",
                "project_no": parts[4].strip() if len(parts) > 4 else "",
                "amount_raw": parts[5].strip() if len(parts) > 5 else "",
                "payment": parts[6].strip() if len(parts) > 6 else "",
                "received_from": parts[7].strip() if len(parts) > 7 else "",
            }
            continue

        # Continuation line (PHP: preg_match('/^\|\s+\|/', $line)); append column 7
        if re.match(r"^\|\s+\|", line) and current_record:
            extra = parts[7].strip() if len(parts) > 7 else ""
            current_record["received_from"] = (current_record.get("received_from") or "") + " " + extra
            continue

    if current_record:
        flush_record(current_record)

    return records


def parse_wallet_recharge_file(
    content: str,
    delimiter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Parse IIC wallet recharge text content.

    If content contains pipe-delimited lines (main row |digit, continuation |\\s+|), uses pipe format.
    Otherwise uses tab/csv with header row.

    Returns:
        List of dicts with keys: sl_no, dated, receipt_no, credited_to_project_no,
        amount, payment_details, received_from, remarks, emp_no, dept_hint, name.
        amount is Decimal; dated is date or None.
    """
    lines = content.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")
    if not lines:
        return []
    if lines[0].startswith("\ufeff"):
        lines[0] = lines[0][1:]

    # Detect pipe format (PHP IICFundParser: main row ^\|\d+\s*\| or continuation ^\|\s+\|)
    is_pipe = any(
        re.match(r"^\|\d+\s*\|", line) or re.match(r"^\|\s+\|", line)
        for line in lines[: min(50, len(lines))]
    )
    if is_pipe:
        return _parse_pipe_format(lines)

    # Detect delimiter from first line
    first = lines[0]
    if delimiter is None:
        delimiter = "\t" if "\t" in first else ","
    else:
        if delimiter == "\\t":
            delimiter = "\t"

    def get_row(line: str) -> List[str]:
        if delimiter == "\t":
            return [c.strip() for c in line.split("\t")]
        return next(csv_reader(StringIO(line), delimiter=delimiter), [])

    header_row = get_row(lines[0])
    col = _header_map(header_row)
    if HEADER_RECEIPT_NO not in col or HEADER_AMOUNT not in col or HEADER_RECEIVED_FROM not in col:
        return []

    required_indices = [col[HEADER_RECEIPT_NO], col[HEADER_AMOUNT], col[HEADER_RECEIVED_FROM]]
    result = []
    for line in lines[1:]:
        row = get_row(line)
        if not row or any(idx >= len(row) for idx in required_indices):
            continue
        receipt_no = (row[col[HEADER_RECEIPT_NO]] or "").strip()
        amount_raw = row[col[HEADER_AMOUNT]] if col.get(HEADER_AMOUNT) is not None else ""
        received_from = (row[col[HEADER_RECEIVED_FROM]] or "").strip() if col.get(HEADER_RECEIVED_FROM) is not None else ""
        if not receipt_no and not amount_raw and not received_from:
            continue
        amount = _parse_amount(amount_raw)
        if amount is None or amount <= 0:
            continue
        dated_raw = row[col[HEADER_DATED]] if col.get(HEADER_DATED) is not None else ""
        dt = _parse_dated(dated_raw)
        sl_no = (row[col[HEADER_SLNO]] or "").strip() if col.get(HEADER_SLNO) is not None else ""
        credited_to_project = (row[col[HEADER_CREDITED_TO_PROJECT]] or "").strip() if col.get(HEADER_CREDITED_TO_PROJECT) is not None else ""
        payment_details = (row[col[HEADER_PAYMENT_DETAILS]] or "").strip() if col.get(HEADER_PAYMENT_DETAILS) is not None else ""
        remarks = (row[col[HEADER_REMARKS]] or "").strip() if col.get(HEADER_REMARKS) is not None else ""

        result.append({
            "sl_no": sl_no,
            "dated": dt.date() if dt else None,
            "receipt_no": receipt_no,
            "credited_to_project_no": credited_to_project,
            "amount": amount,
            "payment_details": payment_details,
            "received_from": received_from,
            "remarks": remarks,
            "emp_no": _extract_emp_no(received_from),
            "dept_hint": _extract_dept_hint(received_from),
            "name": _extract_name(received_from),
        })
    return result


def financial_year_start_for_date(d: datetime) -> "datetime.date":
    """Return April 1 of the financial year for the given date. FY runs April–March."""
    from datetime import date
    if d.month >= 4:
        return date(d.year, 4, 1)
    return date(d.year - 1, 4, 1)
