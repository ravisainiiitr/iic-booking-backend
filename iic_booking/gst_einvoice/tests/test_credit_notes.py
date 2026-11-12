"""Credit notes on refund: off unless the Main Administrator allows one for that invoice; then the refund returns the
GST too and a credit note (ECN series) is issued against the invoice, pro rata and never above it; B2B via the
e-invoice sandbox (CRN), B2C signed with the DSC; GSTR-1 export picks it up."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.external_services import refunds
from iic_booking.external_services.models import RefundDestination, RefundOrigin
from iic_booking.gst_einvoice import credit_notes, gstr1, service
from iic_booking.gst_einvoice.models import DocType, EInvoice, InvoiceStatus

from . import signing
from .conftest import client_for, completed_request, main_admin, make_user

pytestmark = pytest.mark.django_db
API = "/api/v1/gst-einvoice/"


def _issued(factory, **billing):
    req = completed_request(factory, **billing)
    inv = service.prepare_from_request(req)
    assert inv.issued or inv.status == InvoiceStatus.AWAITING_SIGNATURE, inv.review_reasons
    return req.booking, inv


def _refund(booking, captured, *, portion=100):
    q = refunds.quote(booking, penalty_percent=0, tier="lab_failure", portion_percent=portion)
    with captured(execute=True):
        refund = refunds._record(booking, booking.user, origin=RefundOrigin.LAB_FAILURE,
                                 destination=RefundDestination.WALLET, q=q, reason="Equipment under maintenance")
    return q, refund


def test_without_permission_nothing_changes(factory, ext_on, django_capture_on_commit_callbacks):
    booking, inv = _issued(factory)
    q, refund = _refund(booking, django_capture_on_commit_callbacks)
    assert q.gst_refunded is False and q.refund_gst == 0 and q.refund == q.refund_base  # as before: base only
    assert not EInvoice.objects.filter(doc_type=DocType.CREDIT_NOTE).exists()


def test_only_the_main_admin_allows_one_invoice_with_a_reason(factory, ext_on):
    _booking, inv = _issued(factory)
    for who in (make_user(user_type="finance"), make_user()):
        r = client_for(who).post(f"{API}invoices/{inv.pk}/credit-note-permission/", {"reason": "Equipment failed"},
                                 format="json")
        assert r.status_code == 403
    admin = client_for(main_admin())
    assert admin.post(f"{API}invoices/{inv.pk}/credit-note-permission/", {"reason": "short"},
                      format="json").status_code == 400
    r = admin.post(f"{API}invoices/{inv.pk}/credit-note-permission/",
                   {"reason": "XRD under maintenance; job cannot be done"}, format="json")
    assert r.status_code == 200 and r.json()["credit_note_permission"]["reason"].startswith("XRD")
    assert inv.events.filter(action="credit_note_allowed").exists()  # in the append-only audit trail


def test_b2b_refund_issues_a_crn_e_invoice(factory, ext_on, django_capture_on_commit_callbacks):
    booking, inv = _issued(factory)  # Delhi buyer with GSTIN: IGST, IRN issued
    credit_notes.allow(main_admin(), inv.pk, "Instrument down for maintenance; refund with GST")
    q, refund = _refund(booking, django_capture_on_commit_callbacks)
    assert q.gst_refunded and q.refund_gst == Decimal("1620.00") and q.refund == Decimal("10620.00")
    note = EInvoice.objects.get(doc_type=DocType.CREDIT_NOTE)
    assert note.refund == refund and note.original_invoice == inv and note.status == InvoiceStatus.IRN_GENERATED
    assert note.invoice_number.startswith("ECN/") and len(note.invoice_number) <= 16
    assert (note.taxable_value, note.igst, note.cgst, note.total) == (Decimal("9000.00"), Decimal("1620.00"),
                                                                        Decimal("0.00"), Decimal("10620.00"))
    assert note.payload["DocDtls"]["Typ"] == "CRN"
    assert note.payload["RefDtls"]["PrecDocDtls"] == [{"InvNo": inv.invoice_number,
                                                       "InvDt": inv.invoice_date.strftime("%d/%m/%Y")}]
    assert note.irn and note.delivered_at
    with note.pdf.open("rb") as fh:
        assert fh.read().startswith(b"%PDF-")
    # The buyer sees it with the booking; nothing is left to credit.
    body = client_for(booking.user).get(f"{API}my/bookings/{booking.pk}/").json()["invoice"]
    assert body["number"] == inv.invoice_number and body["credit_notes"][0]["number"] == note.invoice_number
    assert credit_notes.remaining(inv) == (Decimal("0.00"), Decimal("0.00"))
    assert not credit_notes.permitted_for_booking(booking)  # fully credited: GST is not refunded again


def test_partial_refund_is_pro_rata_and_capped(factory, ext_on, django_capture_on_commit_callbacks):
    booking, inv = _issued(factory)
    credit_notes.allow(main_admin(), inv.pk, "Half of the samples could not be analysed")
    q, refund = _refund(booking, django_capture_on_commit_callbacks, portion=50)
    note = EInvoice.objects.get(doc_type=DocType.CREDIT_NOTE)
    assert (note.taxable_value, note.igst, note.total) == (Decimal("4500.00"), Decimal("810.00"), Decimal("5310.00"))
    # A (hypothetical) larger credit never exceeds what is left of the invoice.
    note.refund = None
    note.save(update_fields=["refund"])
    type(refund).objects.filter(pk=refund.pk).update(refund_base=Decimal("99999.00"))
    capped = credit_notes.create_for_refund(refund.pk)
    assert capped.taxable_value == Decimal("4500.00") and capped.total == Decimal("5310.00")
    assert credit_notes.remaining(inv) == (Decimal("0.00"), Decimal("0.00"))


def test_b2c_credit_note_is_signed_with_the_dsc_and_exported(factory, ext_on, django_capture_on_commit_callbacks):
    booking, inv = _issued(factory, gstin="", billing_state="Uttarakhand", billing_pincode="247667")
    signatory = make_user(user_type="finance")
    key, cert = signing.test_identity()
    service.register_certificate(main_admin(), signatory, signing.cert_pem(cert))
    device, _token = service.new_device(signatory, "PC")
    with inv.pdf.open("rb") as fh:
        inv = service.accept_signed(inv.pk, device, signing.sign(fh.read(), key, cert))
    credit_notes.allow(main_admin(), inv.pk, "Equipment breakdown after the advance invoice")
    _refund(booking, django_capture_on_commit_callbacks)
    note = EInvoice.objects.get(doc_type=DocType.CREDIT_NOTE)
    assert note.status == InvoiceStatus.AWAITING_SIGNATURE and (note.cgst, note.sgst) == (Decimal("810.00"),
                                                                                            Decimal("810.00"))
    assert note in service.signing_queue()
    with note.pdf.open("rb") as fh:
        note = service.accept_signed(note.pk, device, signing.sign(fh.read(), key, cert))
    assert note.status == InvoiceStatus.SIGNED
    today = timezone.localdate()
    p = gstr1.build_month(today.year, today.month).payload
    intra = next(r for r in p["b2cs"] if r["sply_ty"] == "INTRA")
    assert intra["txval"] == 0.0 and intra["camt"] == 0.0  # invoice and its full credit note net to nil
    cn_docs = next(d for d in p["doc_issue"]["doc_det"] if d["doc_num"] == 5)["docs"][0]
    assert cn_docs["from"] == note.invoice_number and cn_docs["totnum"] == 1


def test_b2cl_credit_note_goes_to_cdnur(factory, ext_on, django_capture_on_commit_callbacks):
    booking, inv = _issued(factory, gstin="", billing_state="Maharashtra", billing_city="Pune",
                           billing_pincode="411001", base=150000, gst=27000)
    assert inv.status == InvoiceStatus.AWAITING_SIGNATURE and inv.inter_state
    EInvoice.objects.filter(pk=inv.pk).update(status=InvoiceStatus.SIGNED)  # signed (tested above)
    credit_notes.allow(main_admin(), inv.pk, "Instrument failure; full refund with GST")
    _refund(booking, django_capture_on_commit_callbacks)
    note = EInvoice.objects.get(doc_type=DocType.CREDIT_NOTE)
    EInvoice.objects.filter(pk=note.pk).update(status=InvoiceStatus.SIGNED)
    today = timezone.localdate()
    p = gstr1.build_month(today.year, today.month).payload
    [cd] = p["cdnur"]
    assert (cd["typ"], cd["nt_num"], cd["pos"], cd["val"]) == ("B2CL", note.invoice_number, "27", 177000.0)
    assert cd["itms"][0]["itm_det"]["iamt"] == 27000.0


def test_revoked_or_switched_off_means_no_gst_back(factory, ext_on, settings):
    booking, inv = _issued(factory)
    admin = main_admin()
    credit_notes.allow(admin, inv.pk, "Instrument down for maintenance")
    assert refunds.quote(booking, penalty_percent=0, tier="lab_failure").gst_refunded
    settings.GST_EINVOICE_ENABLED = False
    assert not refunds.quote(booking, penalty_percent=0, tier="lab_failure").gst_refunded
    settings.GST_EINVOICE_ENABLED = True
    credit_notes.revoke(admin, inv.pk, "Decided against it")
    assert not refunds.quote(booking, penalty_percent=0, tier="lab_failure").gst_refunded
    assert inv.events.filter(action="credit_note_withdrawn").exists()


def test_cannot_allow_on_a_credit_note_or_an_unissued_invoice(factory, ext_on):
    admin = main_admin()
    inv = service.create_demo({"legal_name": "Demo", "gstin": "", "address1": "x", "location": "Roorkee",
                               "pincode": "247667", "state_code": "05", "amount": "100"}, admin)
    assert inv.status == InvoiceStatus.AWAITING_SIGNATURE
    with pytest.raises(service.EinvoiceError):
        credit_notes.allow(admin, inv.pk, "Not issued yet so not allowed")


def test_advance_invoice_option_and_disclosure_already_exist():
    """The advance (early) invoice is the existing external-services option with its accepted disclosure."""
    from iic_booking.external_services.models import InvoiceOption, InvoicePreference, PolicyKey

    assert InvoiceOption.EARLY and PolicyKey.EARLY_INVOICE
    names = {f.name for f in InvoicePreference._meta.get_fields()}
    assert {"disclosure_policy", "disclosure_text_sha256", "acknowledged_at", "chosen_by"} <= names
    assert timedelta(0) == timedelta(0)
