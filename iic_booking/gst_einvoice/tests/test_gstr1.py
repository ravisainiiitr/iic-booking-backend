"""GSTR-1 export of B2C invoices: the builder's JSON against the offline tool's upload shapes (written out below as a
JSON Schema), B2CS / B2CL split at ₹1 lakh, HSN and document summaries, one file per month, Excel sheets, access."""

import io
import json
import zipfile
from datetime import date
from decimal import Decimal

import jsonschema
import pytest
from django.utils import timezone

from iic_booking.gst_einvoice import gstr1, service
from iic_booking.gst_einvoice.models import EInvoice, InvoiceStatus

from .conftest import client_for, main_admin, make_user

pytestmark = pytest.mark.django_db

NUM = {"type": "number"}
POS = {"type": "string", "pattern": "^[0-9]{2}$"}
DATE = {"type": "string", "pattern": "^[0-3][0-9]-[01][0-9]-[0-9]{4}$"}
ITEM = {"type": "object", "required": ["num", "itm_det"], "additionalProperties": False, "properties": {
    "num": {"type": "integer"},
    "itm_det": {"type": "object", "required": ["rt", "txval", "iamt", "csamt"], "additionalProperties": False,
                "properties": {"rt": NUM, "txval": NUM, "iamt": NUM, "csamt": NUM}}}}
# The offline tool's GSTR-1 upload JSON, B2C sections (GSTN publishes no formal schema; shapes as the tool exports).
GSTR1_B2C_SCHEMA = {
    "type": "object", "required": ["gstin", "fp"],
    "properties": {
        "gstin": {"type": "string", "pattern": "^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"},
        "fp": {"type": "string", "pattern": "^(0[1-9]|1[0-2])[0-9]{4}$"},
        "version": {"type": "string"}, "hash": {"type": "string"},
        "b2cs": {"type": "array", "items": {
            "type": "object", "required": ["sply_ty", "pos", "typ", "rt", "txval", "csamt"],
            "additionalProperties": False,
            "properties": {"sply_ty": {"enum": ["INTRA", "INTER"]}, "pos": POS, "typ": {"enum": ["OE", "E"]},
                           "rt": NUM, "txval": NUM, "iamt": NUM, "camt": NUM, "samt": NUM, "csamt": NUM},
            "oneOf": [{"properties": {"sply_ty": {"const": "INTER"}}, "required": ["iamt"],
                       "not": {"anyOf": [{"required": ["camt"]}, {"required": ["samt"]}]}},
                      {"properties": {"sply_ty": {"const": "INTRA"}}, "required": ["camt", "samt"],
                       "not": {"required": ["iamt"]}}]}},
        "b2cl": {"type": "array", "items": {
            "type": "object", "required": ["pos", "inv"], "additionalProperties": False,
            "properties": {"pos": POS, "inv": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["inum", "idt", "val", "itms"], "additionalProperties": False,
                "properties": {"inum": {"type": "string", "maxLength": 16}, "idt": DATE, "val": NUM,
                               "itms": {"type": "array", "minItems": 1, "items": ITEM}}}}}}},
        "cdnur": {"type": "array", "items": {
            "type": "object", "required": ["typ", "ntty", "nt_num", "nt_dt", "val", "itms"],
            "properties": {"typ": {"enum": ["B2CL", "EXPWP", "EXPWOP"]}, "ntty": {"enum": ["C", "D"]},
                           "nt_num": {"type": "string", "maxLength": 16}, "nt_dt": DATE, "val": NUM, "pos": POS,
                           "itms": {"type": "array", "items": ITEM}}}},
        "hsn": {"type": "object", "required": ["hsn_b2c"], "properties": {"hsn_b2c": {"type": "array", "items": {
            "type": "object", "required": ["num", "hsn_sc", "uqc", "qty", "rt", "txval", "iamt", "camt", "samt",
                                           "csamt"],
            "properties": {"num": {"type": "integer"}, "hsn_sc": {"type": "string", "pattern": "^[0-9]{4,8}$"},
                           "desc": {"type": "string"}, "uqc": {"type": "string"}, "qty": NUM, "rt": NUM,
                           "txval": NUM, "iamt": NUM, "camt": NUM, "samt": NUM, "csamt": NUM}}}}},
        "doc_issue": {"type": "object", "required": ["doc_det"], "properties": {"doc_det": {"type": "array", "items": {
            "type": "object", "required": ["doc_num", "docs"],
            "properties": {"doc_num": {"type": "integer"}, "docs": {"type": "array", "items": {
                "type": "object", "required": ["num", "from", "to", "totnum", "cancel", "net_issue"],
                "properties": {"num": {"type": "integer"}, "from": {"type": "string"}, "to": {"type": "string"},
                               "totnum": {"type": "integer"}, "cancel": {"type": "integer"},
                               "net_issue": {"type": "integer"}}}}}}}}},
    },
}


def _demo(admin, **extra):
    data = {"legal_name": "Dr Demo Researcher", "gstin": "", "address1": "Civil Lines", "location": "Roorkee",
            "pincode": "247667", "state_code": "05", "amount": "2400"}
    data.update(extra)
    return service.create_demo(data, admin)


def _signed(inv):
    """As if the signatory's DSC had been accepted (the signing itself is tested in test_flow)."""
    EInvoice.objects.filter(pk=inv.pk).update(status=InvoiceStatus.SIGNED, signed_at=timezone.now())
    return EInvoice.objects.get(pk=inv.pk)


@pytest.fixture
def invoices(db):
    admin = main_admin()
    intra = _signed(_demo(admin))                                                   # UK, 2,400 + 18%: B2CS INTRA
    small = _signed(_demo(admin, legal_name="Small Inst", location="Pune", pincode="411001", state_code="27",
                          amount="50000"))                                          # 59,000 inter: B2CS INTER
    large = _signed(_demo(admin, legal_name="Large Inst", location="Pune", pincode="411001", state_code="27",
                          amount="90000"))                                          # 1,06,200 inter: B2CL
    unsigned = _demo(admin, legal_name="Not Yet Signed")                            # waits for the DSC: excluded
    b2b = _demo(admin, legal_name="Demo Analytics", gstin="07AAACX0000X1Z2", location="New Delhi", pincode="110001",
                amount="9000")                                                      # e-invoice: not in B2C tables
    return {"intra": intra, "small": small, "large": large, "unsigned": unsigned, "b2b": b2b}


def _month():
    today = timezone.localdate()
    return today.year, today.month


def test_month_export_matches_the_gstr1_shapes(invoices):
    y, m = _month()
    e = gstr1.build_month(y, m)
    jsonschema.validate(e.payload, GSTR1_B2C_SCHEMA)
    p = e.payload
    assert p["gstin"] == "05AAALI0033R1Z5" and p["fp"] == f"{m:02d}{y}"
    intra = next(r for r in p["b2cs"] if r["sply_ty"] == "INTRA")
    assert intra == {"sply_ty": "INTRA", "pos": "05", "typ": "OE", "rt": 18, "txval": 2400.0, "camt": 216.0,
                     "samt": 216.0, "csamt": 0}
    inter = next(r for r in p["b2cs"] if r["sply_ty"] == "INTER")
    assert inter == {"sply_ty": "INTER", "pos": "27", "typ": "OE", "rt": 18, "txval": 50000.0, "iamt": 9000.0,
                     "csamt": 0}
    [group] = p["b2cl"]
    [inv] = group["inv"]
    assert group["pos"] == "27" and inv["inum"] == invoices["large"].invoice_number and inv["val"] == 106200.0
    assert inv["idt"] == invoices["large"].invoice_date.strftime("%d-%m-%Y")
    assert inv["itms"] == [{"num": 1, "itm_det": {"rt": 18, "txval": 90000.0, "iamt": 16200.0, "csamt": 0}}]
    [hsn] = p["hsn"]["hsn_b2c"]
    assert (hsn["hsn_sc"], hsn["uqc"], hsn["qty"], hsn["txval"]) == ("998397", "NA", 0, 142400.0)
    assert (hsn["iamt"], hsn["camt"], hsn["samt"]) == (25200.0, 216.0, 216.0)
    # Table 13: the whole series of the month (B2C and B2B), never anything cancelled.
    [doc] = p["doc_issue"]["doc_det"]
    entry = doc["docs"][0]
    assert doc["doc_num"] == 1 and entry["totnum"] == 5 and entry["cancel"] == 0 and entry["net_issue"] == 5
    assert "cdnur" not in p  # no credit notes yet: empty section, said plainly
    assert any("No credit notes" in w for w in e.warnings)
    assert any(invoices["unsigned"].invoice_number in w for w in e.warnings)
    assert e.summary["b2c_invoices"] == 3 and e.summary["unsigned_excluded"] == 1


def test_b2cl_limit_is_one_lakh_and_inclusive_below():
    assert gstr1.B2CL_LIMIT == Decimal("100000.00")


def test_credit_notes_slot_in_when_they_exist(invoices, monkeypatch):
    y, m = _month()
    large, intra = invoices["large"], invoices["intra"]
    today = timezone.localdate()
    notes = [
        gstr1.CreditNoteRow("ECN/26-27/0001", today, large.invoice_number, large.invoice_date, "B2C", True, True,
                            "27", Decimal("10620.00"), [(Decimal("18"), Decimal("9000"), Decimal("1620"), 0, 0)]),
        gstr1.CreditNoteRow("ECN/26-27/0002", today, intra.invoice_number, intra.invoice_date, "B2C", False, False,
                            "05", Decimal("1180.00"), [(Decimal("18"), Decimal("1000"), 0, Decimal("90"),
                                                        Decimal("90"))]),
    ]
    monkeypatch.setattr(gstr1, "credit_note_rows", lambda lo, hi: notes)
    p = gstr1.build_month(y, m).payload
    jsonschema.validate(p, GSTR1_B2C_SCHEMA)
    [cd] = p["cdnur"]
    assert (cd["typ"], cd["ntty"], cd["nt_num"], cd["pos"], cd["val"]) == ("B2CL", "C", "ECN/26-27/0001", "27",
                                                                          10620.0)
    intra_row = next(r for r in p["b2cs"] if r["sply_ty"] == "INTRA")
    assert (intra_row["txval"], intra_row["camt"]) == (1400.0, 126.0)  # netted into B2CS of the note's month
    cn = next(d for d in p["doc_issue"]["doc_det"] if d["doc_num"] == 5)["docs"][0]
    assert (cn["from"], cn["to"], cn["totnum"]) == ("ECN/26-27/0001", "ECN/26-27/0002", 2)


def test_one_file_per_month_and_a_zip_for_several(invoices):
    today = timezone.localdate()
    name, data, ctype = gstr1.export(today.replace(day=1), today, "json")
    assert ctype == "application/json" and name.endswith(f"{today.month:02d}{today.year}-sandbox.json")
    jsonschema.validate(json.loads(data), GSTR1_B2C_SCHEMA)
    prev = (today.replace(day=1) - __import__("datetime").timedelta(days=1)).replace(day=1)
    name, data, ctype = gstr1.export(prev, today, "json")
    assert ctype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        files = sorted(zf.namelist())
        assert len([f for f in files if f.endswith(".json")]) == 2 and "READ-ME.txt" in files
        for f in files:
            if f.endswith(".json"):
                jsonschema.validate(json.loads(zf.read(f)), GSTR1_B2C_SCHEMA)
    with pytest.raises(ValueError):
        gstr1.export(today, prev, "json")
    with pytest.raises(ValueError):
        gstr1.export(prev, today, "pdf")


def test_partial_month_is_warned():
    e = gstr1.build_month(2026, 11, start=date(2026, 11, 5), end=date(2026, 11, 20))
    assert any("not the whole month" in w for w in e.warnings)


def test_excel_has_the_template_sheets_and_headers(invoices):
    from openpyxl import load_workbook

    today = timezone.localdate()
    _name, data, _ctype = gstr1.export(today.replace(day=1), today, "xlsx")
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames[:5] == ["b2cl", "b2cs", "cdnur", "hsn(b2c)", "docs"]
    for sheet, headers in gstr1.FORMAT["excel_sheets"].items():
        assert [c.value for c in wb[sheet][1]] == headers
    b2cl_row = [c.value for c in wb["b2cl"][2]]
    assert b2cl_row[0] == invoices["large"].invoice_number and b2cl_row[3] == "27-Maharashtra"
    assert {r[0].value for r in wb["b2cs"].iter_rows(min_row=2)} == {"OE"}
    assert wb["cdnur"].max_row == 1  # header only: no credit notes yet


def test_api_access(invoices):
    today = timezone.localdate()
    q = f"?from={today.replace(day=1).isoformat()}&to={today.isoformat()}"
    url = "/api/v1/gst-einvoice/gstr1/"
    assert client_for(make_user()).get(url + q).status_code == 403
    reviewer = client_for(make_user(user_type="finance"))
    body = reviewer.get(url + q).json()
    assert body["months"][0]["summary"]["b2cl_invoices"] == 1 and body["b2cl_limit"] == "100000.00"
    r = reviewer.get(url + q + "&kind=xlsx")
    assert r.status_code == 200 and r["Content-Type"].startswith("application/vnd.openxmlformats")
    assert reviewer.get(url + "?from=x&to=y").status_code == 400
    # The signing utility, with its device token only.
    from . import signing

    signatory = make_user(user_type="finance")
    _k, cert = signing.test_identity()
    service.register_certificate(main_admin(), signatory, signing.cert_pem(cert))
    _device, token = service.new_device(signatory, "PC")
    tool = client_for()
    assert tool.get("/api/v1/gst-einvoice/signer/api/gstr1/" + q).status_code == 401
    r = tool.get("/api/v1/gst-einvoice/signer/api/gstr1/" + q + "&kind=json", HTTP_AUTHORIZATION=f"SricSigner {token}")
    assert r.status_code == 200 and json.loads(r.content)["fp"] == f"{today.month:02d}{today.year}"
