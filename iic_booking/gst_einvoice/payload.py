"""The e-invoice JSON (NIC schema version 1.1) for an approved invoice, and the checks the IRP applies to it.

Field names and rules follow the NIC sandbox "Generate IRN" page (v1.03, schema 1.1): DocDtls.No 1-16 characters
matching ^[a-zA-Z1-9][a-zA-Z0-9/-]{0,15}$, dates DD/MM/YYYY, HSN/SAC 4, 6 or 8 digits, CGST = SGST for intra-state
supplies, IGST for inter-state, totals equal to the sum of the lines (within the IRP's tolerance).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal

from .gstin import validate as validate_gstin

SCHEMA_VERSION = "1.1"
DOC_NO = re.compile(r"^[a-zA-Z1-9][a-zA-Z0-9/-]{0,15}$")
SAC = re.compile(r"^(\d{4}|\d{6}|\d{8})$")
PIN = re.compile(r"^[1-9]\d{5}$")


def _num(value) -> float:
    return float(Decimal(str(value)))


def _d(day) -> str:
    return day.strftime("%d/%m/%Y")


def build(invoice) -> dict:
    """The schema-1.1 JSON for ``invoice`` (an approved B2B EInvoice with its lines and customer)."""
    from .models import EinvoiceSettings

    s = EinvoiceSettings.get()
    c = invoice.customer
    payload = {
        "Version": SCHEMA_VERSION,
        "TranDtls": {"TaxSch": "GST", "SupTyp": "B2B", "RegRev": "N", "IgstOnIntra": "N"},
        "DocDtls": {"Typ": invoice.doc_type or "INV", "No": invoice.invoice_number, "Dt": _d(invoice.invoice_date)},
        "SellerDtls": {
            "Gstin": s.supplier_gstin, "LglNm": s.supplier_legal_name[:100], "TrdNm": s.supplier_trade_name[:100],
            "Addr1": s.supplier_address1[:100], "Loc": s.supplier_location[:50], "Pin": int(s.supplier_pincode),
            "Stcd": s.supplier_state_code, "Em": s.supplier_email,
        },
        "BuyerDtls": {
            "Gstin": c.gstin, "LglNm": c.legal_name[:100], "Pos": invoice.place_of_supply,
            "Addr1": c.address1[:100], "Loc": c.location[:50], "Pin": int(c.pincode), "Stcd": c.state_code,
        },
        "ItemList": [
            {
                "SlNo": str(line.sl_no), "PrdDesc": (f"{line.description} - {line.detail}" if line.detail else line.description)[:300],
                "IsServc": "Y", "HsnCd": line.sac_code, "Qty": _num(line.quantity), "Unit": line.unit,
                "UnitPrice": _num(line.unit_price), "TotAmt": _num(line.taxable_value), "Discount": 0,
                "AssAmt": _num(line.taxable_value), "GstRt": _num(line.gst_rate), "IgstAmt": _num(line.igst),
                "CgstAmt": _num(line.cgst), "SgstAmt": _num(line.sgst), "TotItemVal": _num(line.total),
            }
            for line in invoice.lines.all()
        ],
        "ValDtls": {
            "AssVal": _num(invoice.taxable_value), "CgstVal": _num(invoice.cgst), "SgstVal": _num(invoice.sgst),
            "IgstVal": _num(invoice.igst), "RndOffAmt": _num(invoice.round_off), "TotInvVal": _num(invoice.total),
        },
    }
    if c.trade_name:
        payload["BuyerDtls"]["TrdNm"] = c.trade_name[:100]
    if c.address2:
        payload["BuyerDtls"]["Addr2"] = c.address2[:100]
    if c.email:
        payload["BuyerDtls"]["Em"] = c.email
    if invoice.po_number:
        ref = {"PORefr": invoice.po_number[:16]}
        if invoice.po_date:
            ref["PORefDt"] = _d(invoice.po_date)
        payload["RefDtls"] = {"ContrDtls": [ref]}
    if invoice.doc_type == "CRN" and invoice.original_invoice_id:
        orig = invoice.original_invoice
        payload.setdefault("RefDtls", {})["PrecDocDtls"] = [{"InvNo": orig.invoice_number, "InvDt": _d(orig.invoice_date)}]
    if invoice.receipt_number:
        # The payment receipt the invoice refers to (free-text remark; the IRP keeps it with the invoice).
        payload.setdefault("RefDtls", {})["InvRm"] = f"Paid: receipt {invoice.receipt_number}"[:100]
    return payload


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _tolerant(passed: float, calculated: Decimal) -> bool:
    """IRP tolerance: passed value within [floor(calc) - 1, ceil(calc) + 1]."""
    low = math.floor(calculated) - 1
    high = math.ceil(calculated) + 1
    return low <= passed <= high


def validate(payload: dict) -> list[str]:
    """Problems that would make the IRP refuse the payload (empty list when it may be sent)."""
    errors: list[str] = []
    doc = payload.get("DocDtls", {})
    if not DOC_NO.match(str(doc.get("No", ""))):
        errors.append("Invoice number must be 1-16 characters (letters, digits, / and -) and not start with 0, / or -.")
    if not re.match(r"^\d{2}/\d{2}/\d{4}$", str(doc.get("Dt", ""))):
        errors.append("Invoice date must be DD/MM/YYYY.")
    for side in ("SellerDtls", "BuyerDtls"):
        details = payload.get(side, {})
        ok, why = validate_gstin(details.get("Gstin", ""))
        if not ok:
            errors.append(f"{'Supplier' if side == 'SellerDtls' else 'Recipient'} GSTIN: {why}")
        for key in ("LglNm", "Addr1", "Loc", "Stcd"):
            if not str(details.get(key, "")).strip():
                errors.append(f"{side}.{key} is required.")
        if not PIN.match(str(details.get("Pin", ""))):
            errors.append(f"{side}.Pin must be a 6-digit PIN code.")
    if payload.get("SellerDtls", {}).get("Gstin") == payload.get("BuyerDtls", {}).get("Gstin"):
        errors.append("Supplier and recipient GSTIN cannot be the same.")
    items = payload.get("ItemList") or []
    if not 1 <= len(items) <= 1000:
        errors.append("An invoice needs 1 to 1000 items.")
    inter = payload.get("SellerDtls", {}).get("Stcd") != payload.get("BuyerDtls", {}).get("Pos")
    sums = {"AssVal": Decimal(0), "CgstVal": Decimal(0), "SgstVal": Decimal(0), "IgstVal": Decimal(0), "TotItemVal": Decimal(0)}
    for item in items:
        n = item.get("SlNo")
        if not SAC.match(str(item.get("HsnCd", ""))):
            errors.append(f"Item {n}: SAC must have 4, 6 or 8 digits.")
        ass = Decimal(str(item.get("AssAmt", 0)))
        rate = Decimal(str(item.get("GstRt", 0)))
        if inter:
            if item.get("CgstAmt") or item.get("SgstAmt"):
                errors.append(f"Item {n}: an inter-state supply carries IGST, not CGST/SGST.")
            if not _tolerant(item.get("IgstAmt", 0), ass * rate / 100):
                errors.append(f"Item {n}: IGST does not match taxable value x rate.")
        else:
            if item.get("IgstAmt"):
                errors.append(f"Item {n}: an intra-state supply carries CGST and SGST, not IGST.")
            if item.get("CgstAmt") != item.get("SgstAmt"):
                errors.append(f"Item {n}: CGST and SGST must be equal.")
            if not _tolerant(item.get("CgstAmt", 0), ass * rate / 200):
                errors.append(f"Item {n}: CGST does not match taxable value x rate / 2.")
        expected_total = ass + Decimal(str(item.get("CgstAmt", 0))) + Decimal(str(item.get("SgstAmt", 0))) + Decimal(str(item.get("IgstAmt", 0)))
        if not _tolerant(item.get("TotItemVal", 0), expected_total):
            errors.append(f"Item {n}: item total does not add up.")
        sums["AssVal"] += ass
        sums["CgstVal"] += Decimal(str(item.get("CgstAmt", 0)))
        sums["SgstVal"] += Decimal(str(item.get("SgstAmt", 0)))
        sums["IgstVal"] += Decimal(str(item.get("IgstAmt", 0)))
        sums["TotItemVal"] += Decimal(str(item.get("TotItemVal", 0)))
    val = payload.get("ValDtls", {})
    for key in ("AssVal", "CgstVal", "SgstVal", "IgstVal"):
        if not _tolerant(val.get(key, 0), sums[key]):
            errors.append(f"ValDtls.{key} does not equal the sum of the items.")
    if abs(Decimal(str(val.get("RndOffAmt", 0)))) > Decimal("99.99"):
        errors.append("Round-off must be within ±99.99.")
    if not _tolerant(val.get("TotInvVal", 0), sums["TotItemVal"] + Decimal(str(val.get("RndOffAmt", 0)))):
        errors.append("ValDtls.TotInvVal does not equal the item totals plus round-off.")
    po = (payload.get("RefDtls") or {}).get("ContrDtls") or []
    for ref in po:
        if not 1 <= len(str(ref.get("PORefr", ""))) <= 16:
            errors.append("PO reference must be 1-16 characters.")
    return errors


SECRET_KEYS = {"password", "Password", "client_secret", "AppKey", "appkey", "Sek", "AuthToken", "authtoken"}


def redact(data):
    """Copy of request/response data with secrets removed and contact details masked, for the attempt log."""
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if key in SECRET_KEYS:
                out[key] = "***"
            elif key in ("Em", "Ph", "email", "phone"):
                out[key] = "***" if value else value
            elif key in ("SignedInvoice", "SignedQRCode") and isinstance(value, str):
                out[key] = f"<signed JWT, {len(value)} characters>"
            else:
                out[key] = redact(value)
        return out
    if isinstance(data, list):
        return [redact(v) for v in data]
    return data
