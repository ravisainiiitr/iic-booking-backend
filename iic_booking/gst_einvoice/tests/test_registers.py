"""Monthly Invoice Register and Credit Note Register (Excel) next to the GSTR-1 JSON, for B2C documents."""

import io
import json
import zipfile
from decimal import Decimal

import pytest
from django.utils import timezone
from openpyxl import load_workbook

from iic_booking.gst_einvoice import credit_notes, gstr1, registers, service
from iic_booking.gst_einvoice.models import DocType, EInvoice

from . import signing
from .conftest import client_for, main_admin, make_user
from .test_credit_notes import _issued, _refund
from .test_gstr1 import GSTR1_B2C_SCHEMA, invoices  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


def _sheet(data):
    wb = load_workbook(io.BytesIO(data))
    ws = wb.worksheets[0]
    headers = [c.value for c in ws[3]]
    rows = [dict(zip(headers, (c.value for c in r))) for r in ws.iter_rows(min_row=4)]
    return wb, headers, rows[:-1], rows[-1]


def _this_month():
    today = timezone.localdate()
    return today.replace(day=1), today


def test_invoice_register_lists_every_b2c_invoice_and_reconciles_with_the_json(invoices):  # noqa: F811
    start, end = _this_month()
    name, data, ctype = gstr1.export(start, end, "invoice_register")
    assert name.startswith("InvoiceRegister-B2C-05AAALI0033R1Z5-") and ctype.startswith("application/vnd.openxml")
    wb, headers, rows, total = _sheet(data)
    assert headers == [h for h, _w in registers.INVOICE_COLUMNS] and "read me" in wb.sheetnames
    by_no = {r["Invoice No."]: r for r in rows}
    # B2C only: the B2B e-invoice is not listed; the unsigned one is listed but not in the JSON.
    assert invoices["b2b"].invoice_number not in by_no and len(rows) == 4
    assert by_no[invoices["unsigned"].invoice_number]["In GSTR-1 JSON"] == "No"
    large = by_no[invoices["large"].invoice_number]
    assert (large["GSTR-1 Table"], large["Place of Supply"], large["Supply"], large["IGST"]) == (
        "B2CL", "27-Maharashtra", "Inter-state", 16200.0)
    assert by_no[invoices["intra"].invoice_number]["GSTR-1 Table"] == "B2CS"
    assert by_no[invoices["intra"].invoice_number]["SAC"] == "998397"
    # Totals add up the signed invoices only: the same taxable value as the JSON's B2CS + B2CL.
    p = json.loads(gstr1.export(start, end, "json")[1])
    json_taxable = sum(r["txval"] for r in p["b2cs"]) + sum(
        it["itm_det"]["txval"] for g in p["b2cl"] for inv in g["inv"] for it in inv["itms"])
    assert total["Taxable Value"] == json_taxable == 142400.0
    assert total["Invoice Value"] == 2832.0 + 59000.0 + 106200.0


def test_credit_note_register_gives_the_original_invoice(factory, ext_on, django_capture_on_commit_callbacks):
    booking, inv = _issued(factory, gstin="", billing_state="Uttarakhand", billing_pincode="247667")
    signatory = make_user(user_type="finance")
    key, cert = signing.test_identity()
    service.register_certificate(main_admin(), signatory, signing.cert_pem(cert))
    device, _token = service.new_device(signatory, "PC")
    with inv.pdf.open("rb") as fh:
        inv = service.accept_signed(inv.pk, device, signing.sign(fh.read(), key, cert))
    credit_notes.allow(main_admin(), inv.pk, "Equipment breakdown after the advance invoice")
    _q, refund = _refund(booking, django_capture_on_commit_callbacks, portion=50)
    note = EInvoice.objects.get(doc_type=DocType.CREDIT_NOTE)
    start, end = _this_month()

    # Before the DSC: listed, not in the JSON, and the JSON warns.
    _wb, _h, [row], total = _sheet(gstr1.export(start, end, "credit_note_register")[1])
    assert row["Credit Note No."] == note.invoice_number and row["In GSTR-1 JSON"] == "No"
    assert total["Credit Note Value"] in (None, 0, 0.0)
    assert any(note.invoice_number in w for w in gstr1.build_month(start.year, start.month).warnings)

    with note.pdf.open("rb") as fh:
        service.accept_signed(note.pk, device, signing.sign(fh.read(), key, cert))
    name, data, _ = gstr1.export(start, end, "credit_note_register")
    assert name.startswith("CreditNoteRegister-B2C-")
    _wb, headers, [row], total = _sheet(data)
    assert headers == [h for h, _w in registers.CREDIT_NOTE_COLUMNS]
    assert (row["Original Invoice No."], row["Original Invoice Value"], row["Original GSTR-1 Table"]) == (
        inv.invoice_number, float(inv.total), "B2CS")
    assert row["Original Return Period"] == inv.invoice_date.strftime("%m%Y")
    assert (row["In GSTR-1 JSON"], row["GSTR-1 Treatment"]) == ("Yes", "Netted in B2CS")
    assert (row["CGST"], row["SGST"], row["IGST"]) == (405.0, 405.0, 0.0)
    assert row["Reason"].startswith(f"Refund of {note.total}") and row["Refund Ref."] == refund.pk
    assert row["Signed By"] and row["DSC Certificate Serial"]
    assert total["Credit Note Value"] == float(note.total)

    # The invoice register shows what was credited against the invoice.
    _wb, _h, [irow], _t = _sheet(gstr1.export(start, end, "invoice_register")[1])
    assert irow["Credit Notes Issued"] == note.invoice_number
    assert Decimal(str(irow["Net Value"])) == inv.total - note.total


def test_all_files_per_month_in_one_zip(invoices):  # noqa: F811
    import jsonschema

    start, end = _this_month()
    name, data, ctype = gstr1.export(start, end, "all")
    fp = f"{end.month:02d}{end.year}"
    assert ctype == "application/zip" and name == f"GSTR1-and-registers-B2C-05AAALI0033R1Z5-{fp}-sandbox.zip"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        files = sorted(zf.namelist())
        assert files == sorted([f"CreditNoteRegister-B2C-05AAALI0033R1Z5-{fp}-sandbox.xlsx",
                                f"GSTR1-B2C-05AAALI0033R1Z5-{fp}-sandbox.json",
                                f"InvoiceRegister-B2C-05AAALI0033R1Z5-{fp}-sandbox.xlsx", "READ-ME.txt", f"B2B-EInvoiceRegister-05AAALI0033R1Z5-{fp}-sandbox.xlsx"])
        jsonschema.validate(json.loads(zf.read(f"GSTR1-B2C-05AAALI0033R1Z5-{fp}-sandbox.json")), GSTR1_B2C_SCHEMA)
        _wb, _h, rows, _t = _sheet(zf.read(f"CreditNoteRegister-B2C-05AAALI0033R1Z5-{fp}-sandbox.xlsx"))
        assert rows == []  # header and an empty totals row: no credit notes this month
    prev = (start - timezone.timedelta(days=1)).replace(day=1)
    _name, data, _ = gstr1.export(prev, end, "all")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert len([f for f in zf.namelist() if f != "READ-ME.txt"]) == 8  # four per month


def test_signing_utility_downloads_the_registers(invoices):  # noqa: F811
    signatory = make_user(user_type="finance")
    _k, cert = signing.test_identity()
    service.register_certificate(main_admin(), signatory, signing.cert_pem(cert))
    _device, token = service.new_device(signatory, "PC")
    start, end = _this_month()
    url = f"/api/v1/gst-einvoice/signer/api/gstr1/?from={start.isoformat()}&to={end.isoformat()}"
    tool = client_for()
    for kind, stem in (("invoice_register", "InvoiceRegister"), ("credit_note_register", "CreditNoteRegister"),
                       ("b2b_register", "B2B-EInvoiceRegister"), ("all", "GSTR1-and-registers")):
        r = tool.get(f"{url}&kind={kind}", HTTP_AUTHORIZATION=f"SricSigner {token}")
        assert r.status_code == 200 and stem in r["Content-Disposition"], kind
    assert tool.get(f"{url}&kind=pdf", HTTP_AUTHORIZATION=f"SricSigner {token}").status_code == 400
    body = client_for(make_user(user_type="finance")).get(
        f"/api/v1/gst-einvoice/gstr1/?from={start.isoformat()}&to={end.isoformat()}").json()
    assert set(body["kinds"]) == set(gstr1.KINDS)


def test_b2b_register_lists_e_invoices_to_verify_gstr1(invoices):  # noqa: F811
    start, end = _this_month()
    name, data, _ = gstr1.export(start, end, "b2b_register")
    assert name.startswith("B2B-EInvoiceRegister-05AAALI0033R1Z5-")
    wb, headers, rows, total = _sheet(data)
    assert headers == [h for h, _w in registers.B2B_COLUMNS]
    [row] = rows  # only the B2B e-invoice; no B2C invoice is listed
    b2b = EInvoice.objects.get(pk=invoices["b2b"].pk)
    assert (row["Document No."], row["Buyer GSTIN"]) == (b2b.invoice_number, "07AAACX0000X1Z2")
    assert b2b.irn and row["IRN Generated"] == "Yes"
    assert row["Expected in GSTR-1 Table"] == "4A B2B" and row["IRN"] == b2b.irn
    assert row["Sandbox Mock (not a real IRN)"] == "Yes" and total["Document Value"] == float(b2b.total)
    assert row["Found in GSTR-1 (Y/N)"] is None  # for SRIC to fill while comparing
    assert any("auto-populated GSTR-1" in (r[0].value or "") for r in wb["read me"].iter_rows())


def test_b2b_credit_note_is_expected_in_cdnr(factory, ext_on, django_capture_on_commit_callbacks):
    booking, inv = _issued(factory)  # Delhi buyer with a GSTIN: IRN from the mock
    credit_notes.allow(main_admin(), inv.pk, "Instrument down for maintenance; refund with GST")
    _refund(booking, django_capture_on_commit_callbacks)
    note = EInvoice.objects.get(doc_type=DocType.CREDIT_NOTE)
    start, end = _this_month()
    _wb, _h, rows, total = _sheet(gstr1.export(start, end, "b2b_register")[1])
    by_no = {r["Document No."]: r for r in rows}
    crn = by_no[note.invoice_number]
    assert (crn["Document Type"], crn["Expected in GSTR-1 Table"], crn["Original Invoice No."]) == (
        "Credit Note (CRN)", "9B CDNR", inv.invoice_number)
    assert by_no[inv.invoice_number]["Expected in GSTR-1 Table"] == "4A B2B"
    assert total["Document Value"] == 0.0  # invoice less its full credit note
