"""The flow on top of the existing SRIC invoice request: B2B with an IRN (mock sandbox), B2C signed with a DSC,
review rules, no renumbering after an unanswered call, delivery as the request's official document, the weekly
batch skipping portal-invoiced requests, and the monthly register."""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from iic_booking.external_services.models import InvoiceRequestStatus, OfficialInvoiceDocument, SricInvoiceRequest
from iic_booking.gst_einvoice import service
from iic_booking.gst_einvoice.models import (
    EInvoice,
    EinvoiceSettings,
    InvoiceStatus,
    IrpAttempt,
    SignerCertificate,
    SimulatedIrpRecord,
    SupplyType,
)

from . import signing
from .conftest import completed_request, main_admin, make_user

pytestmark = pytest.mark.django_db
IST = ZoneInfo("Asia/Kolkata")


def _pdf(inv):
    with (inv.signed_pdf if inv.status == InvoiceStatus.SIGNED else inv.pdf).open("rb") as fh:
        return fh.read()


def test_b2b_gets_an_irn_and_reaches_the_buyer(factory, ext_on):
    req = completed_request(factory)
    inv = service.prepare_from_request(req)
    assert inv.supply_type == SupplyType.B2B and inv.status == InvoiceStatus.IRN_GENERATED, inv.review_reasons
    assert inv.invoice_number.startswith("EBP/") and inv.invoice_number.endswith("/0001")
    # Delhi buyer from Uttarakhand: IGST; SAC 998397 at 18%; same total as the booking charged.
    assert inv.inter_state and inv.igst == Decimal("1620.00") and inv.cgst == inv.sgst == Decimal("0.00")
    assert inv.total == Decimal("10620.00") == inv.portal_total
    assert inv.lines.get().sac_code == "998397"
    assert inv.irn.startswith("SIMULATED-") and inv.simulated and inv.signed_qr
    assert inv.payload["DocDtls"]["No"] == inv.invoice_number and inv.payload["SellerDtls"]["Gstin"] == "05AAALI0033R1Z5"
    assert _pdf(inv).startswith(b"%PDF-")
    # Delivered through the existing flow: the request's official document, request marked received.
    req.refresh_from_db()
    assert req.status == InvoiceRequestStatus.INVOICE_RECEIVED
    doc = OfficialInvoiceDocument.objects.get(request=req, is_current=True)
    assert doc.invoice_number == inv.invoice_number and inv.official_document_id == doc.pk
    # Same request again: same invoice, no second number.
    assert service.prepare_from_request(req).pk == inv.pk and EInvoice.objects.count() == 1


def test_b2c_waits_for_the_dsc_then_is_delivered(factory, ext_on):
    req = completed_request(factory, gstin="", billing_state="Uttarakhand", billing_city="Roorkee",
                            billing_pincode="247667")
    inv = service.prepare_from_request(req)
    assert inv.supply_type == SupplyType.B2C and inv.status == InvoiceStatus.AWAITING_SIGNATURE
    assert not inv.inter_state and inv.cgst == inv.sgst == Decimal("810.00") and inv.irn == ""
    assert inv.invoice_number and inv.pdf_sha256
    signatory = make_user(user_type="finance")
    key, cert = signing.test_identity()
    service.register_certificate(main_admin(), signatory, signing.cert_pem(cert), is_test=True)
    device, token = service.new_device(signatory, "SRIC office PC")
    assert service.device_for_token(token) == device and service.device_for_token(token + "x") is None
    signed = signing.sign(_pdf(inv), key, cert)
    inv = service.accept_signed(inv.pk, device, signed)
    assert inv.status == InvoiceStatus.SIGNED and inv.signed_by == signatory
    assert inv.signer_certificate.sha256_fingerprint == cert.fingerprint(__import__("cryptography").hazmat.primitives.hashes.SHA256()).hex()
    req.refresh_from_db()
    assert req.status == InvoiceRequestStatus.INVOICE_RECEIVED
    with req.documents.get(is_current=True).file.open("rb") as fh:
        assert fh.read() == signed  # the buyer gets the signed file itself


def _b2c_waiting(factory):
    req = completed_request(factory, gstin="", billing_state="Uttarakhand", billing_pincode="247667")
    inv = service.prepare_from_request(req)
    signatory = make_user(user_type="finance")
    key, cert = signing.test_identity()
    cert_row = service.register_certificate(main_admin(), signatory, signing.cert_pem(cert))
    device, _token = service.new_device(signatory, "PC")
    return inv, device, key, cert, cert_row


@pytest.mark.parametrize("case", ["tampered", "other_key", "overlay", "changed_page", "deactivated", "outside_box"])
def test_signed_pdf_is_refused_unless_it_is_exactly_the_prepared_invoice(factory, ext_on, case):
    inv, device, key, cert, cert_row = _b2c_waiting(factory)
    prepared = _pdf(inv)
    if case == "tampered":
        signed = bytearray(signing.sign(prepared, key, cert))
        signed[len(prepared) // 2] ^= 0x01
        signed = bytes(signed)
    elif case == "other_key":
        k2, c2 = signing.test_identity()
        signed = signing.sign(prepared, k2, c2)
    elif case == "overlay":
        signed = signing.sign(prepared, key, cert, extra_annotation=True)
    elif case == "changed_page":
        signed = signing.sign(prepared, key, cert, change_content=True)
    elif case == "outside_box":
        signed = signing.sign(prepared, key, cert, box=(40, 400, 300, 440))
    else:
        service.deactivate_certificate(main_admin(), cert_row.pk, "Token reported lost")
        signed = signing.sign(prepared, key, cert)
    with pytest.raises(service.EinvoiceError) as err:
        service.accept_signed(inv.pk, device, signed)
    assert err.value.code in ("signature_rejected",)
    inv.refresh_from_db()
    assert inv.status == InvoiceStatus.AWAITING_SIGNATURE and not inv.signed_pdf
    assert inv.events.filter(action="signature_refused").exists()


def test_review_rules_hold_without_using_a_number(factory, ext_on):
    s = EinvoiceSettings.get()
    s.review_above_amount = Decimal("5000")
    s.save()
    req = completed_request(factory)
    inv = service.prepare_from_request(req)
    assert inv.status == InvoiceStatus.PENDING_REVIEW and not inv.invoice_number
    assert any("review limit" in r for r in inv.review_reasons)
    reviewer = make_user(user_type="finance")
    inv = service.approve(inv.pk, reviewer, "Checked against the PO")
    assert inv.status == InvoiceStatus.IRN_GENERATED and inv.invoice_number.endswith("/0001")
    assert inv.reviewed_by == reviewer


def test_rejected_invoice_goes_back_to_the_manual_route(factory, ext_on):
    req = completed_request(factory, gstin="07AAACX0000X1Z9")  # wrong check digit
    inv = service.prepare_from_request(req)
    assert inv.status == InvoiceStatus.PENDING_REVIEW and any("GSTIN" in r for r in inv.review_reasons)
    inv = service.reject(inv.pk, make_user(user_type="finance"), "Buyer to confirm GSTIN")
    assert inv.status == InvoiceStatus.REJECTED and not inv.invoice_number
    from django.db.models import Q

    pending = SricInvoiceRequest.objects.filter(Q(gst_einvoice__isnull=True) | Q(gst_einvoice__status="rejected"))
    assert req in pending


def test_weekly_batch_does_not_email_requests_the_portal_invoices(factory, ext_on):
    from iic_booking.external_services import sric_dispatch

    ext_on.sric_dispatch_mode, ext_on.sric_test_recipient_email = "test", "sric-test@example.org"
    ext_on.save()
    portal = completed_request(factory)
    service.prepare_from_request(portal)
    SricInvoiceRequest.objects.filter(pk=portal.pk).update(status=InvoiceRequestStatus.QUEUED)  # as if still queued
    manual = completed_request(factory, test_account=False)
    SricInvoiceRequest.objects.update(created_at=timezone.now() - timedelta(days=2))
    d = timezone.localdate() + timedelta(days=1)
    while not sric_dispatch.is_batch_day(d):
        d += timedelta(days=1)
    result = sric_dispatch.run_batch(datetime(d.year, d.month, d.day, 9, 30, tzinfo=IST))
    assert result["batched"] == 1
    manual.refresh_from_db()
    portal.refresh_from_db()
    assert manual.status == InvoiceRequestStatus.SENT and portal.status == InvoiceRequestStatus.QUEUED


def test_sandbox_takes_up_test_accounts_only(factory, ext_on):
    req = completed_request(factory, test_account=False)
    with pytest.raises(service.EinvoiceError) as err:
        service.prepare_from_request(req)
    assert err.value.code == "not_eligible"
    assert service.process_pending()["prepared"] == 0


def test_off_switch_stops_everything(factory, ext_on, settings):
    settings.GST_EINVOICE_ENABLED = False
    req = completed_request(factory)
    with pytest.raises(service.EinvoiceError):
        service.prepare_from_request(req)
    assert service.process_pending() == {"skipped": "disabled"}


def test_new_request_is_offered_after_commit(factory, ext_on, monkeypatch, django_capture_on_commit_callbacks):
    from iic_booking.gst_einvoice import tasks

    queued = []
    monkeypatch.setattr(tasks.prepare_request, "delay", lambda pk: queued.append(pk))
    with django_capture_on_commit_callbacks(execute=True):
        req = completed_request(factory)
    assert queued == [req.pk]
    assert tasks.prepare_request(req.pk).endswith(InvoiceStatus.IRN_GENERATED)


def _demo(user, **extra):
    data = {"legal_name": "Demo Analytics Pvt Ltd", "gstin": "07AAACX0000X1Z2", "address1": "1 Demo Street",
            "location": "New Delhi", "pincode": "110001", "amount": "9000", "description": "XRD analysis"}
    data.update(extra)
    return service.create_demo(data, user)


def test_unanswered_call_is_looked_up_never_renumbered(factory):
    admin = main_admin()
    inv = _demo(admin, notes="#simulate-timeout")
    assert inv.status == InvoiceStatus.SUBMISSION_UNCERTAIN and inv.invoice_number
    number = inv.invoice_number
    inv = service.submit_irn(inv.pk, admin)
    assert inv.status == InvoiceStatus.IRN_GENERATED and inv.invoice_number == number
    assert SimulatedIrpRecord.objects.filter(doc_number=number).count() == 1
    ops = list(IrpAttempt.objects.filter(invoice=inv).order_by("created_at", "pk").values_list("operation", "outcome"))
    assert ops == [("generate", "uncertain"), ("get_by_doc", "success")]


def test_refusal_keeps_the_number_and_can_be_corrected(factory):
    admin = main_admin()
    inv = _demo(admin, notes="#simulate-refusal")
    assert inv.status == InvoiceStatus.SUBMISSION_FAILED
    number = inv.invoice_number
    # The buyer turns out to have no GSTIN: same number, now a tax invoice for the DSC.
    inv = service.update_buyer(inv.pk, admin, {"gstin": "", "state_code": "07"})
    assert inv.invoice_number == number and inv.supply_type == SupplyType.B2C
    assert inv.status == InvoiceStatus.AWAITING_SIGNATURE


def test_gstin_check_with_the_sandbox(factory):
    admin = main_admin()
    s = EinvoiceSettings.get()
    s.auto_issue = False
    s.save()
    inv = _demo(admin)
    assert inv.status == InvoiceStatus.PENDING_REVIEW
    inv = service.check_gstin(inv.pk, admin)
    assert inv.customer.gstin_status == "simulated_verified"


def test_register_lists_issued_invoices_and_the_series(factory, ext_on):
    admin = main_admin()
    a = _demo(admin)
    b = _demo(admin, gstin="", location="Roorkee", pincode="247667", state_code="05", amount="1000")
    today = timezone.localdate()
    data = service.register(today.year, today.month)
    assert [r["invoice_number"] for r in data["rows"]] == [a.invoice_number, b.invoice_number]
    assert data["series"][0]["count"] == 2 and data["series"][0]["from"] == a.invoice_number
    assert data["counts"] == {"b2b": 1, "b2c": 0, "not_yet_issued": 1}  # the B2C one waits for its signature
    assert data["totals"]["igst"] == "1620.00"


def test_no_cancellation_operation_exists():
    from iic_booking.gst_einvoice.providers.base import ALL_OPERATIONS, IrpProvider

    assert "cancel" not in ALL_OPERATIONS and not hasattr(IrpProvider, "cancel_irn")
    assert "cancelled" not in InvoiceStatus.values
    assert not SignerCertificate._meta.get_field("certificate_pem").null
