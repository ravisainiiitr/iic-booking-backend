"""REST API for portal GST invoices (sandbox). Mounted at /api/v1/gst-einvoice/.

Staff (SRIC reviewers: Main Administrator, Accounts In Charge, named SRIC accounts): list, review, send, register.
Main Administrator: settings, tax rules, signatories' certificates.
Signatories: link a signing utility (device token, shown once).
Signing utility (``Authorization: SricSigner <token>``, no session or cookie): the queue, the prepared PDFs, and the
upload of signed PDFs. Nothing else is reachable with a device token.
Buyers: their booking's invoice status.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from . import conf, dsc, service
from .models import EInvoice, EinvoiceSettings, InvoiceStatus, SignerCertificate, SignerDevice, TaxRule
from .service import EinvoiceError


def _handled(fn):
    @wraps(fn)
    def inner(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except EinvoiceError as exc:
            return Response(exc.payload(), status=exc.status)
        except conf.ProductionEndpointRefused as exc:
            return Response({"error": str(exc), "code": "production_refused"}, status=403)

    return inner


def _invoice(pk) -> EInvoice:
    inv = EInvoice.objects.select_related("customer", "signer_certificate", "signed_by").filter(pk=pk).first()
    if inv is None:
        raise EinvoiceError("Invoice not found.", status=404, code="not_found")
    return inv


def _pdf_response(data: bytes, name: str) -> HttpResponse:
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{name}"'
    resp["Cache-Control"] = "no-store"
    return resp


# --- status ------------------------------------------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def status_view(request):
    s = EinvoiceSettings.get() if conf.enabled() else None
    body = {
        **conf.status(),
        "can_review": conf.enabled() and service.can_review(request.user),
        "can_administer": conf.enabled() and service.can_administer(request.user),
        "is_signatory": conf.enabled() and service.is_signatory(request.user),
    }
    if s is not None and (body["can_review"] or body["is_signatory"]):
        body["settings"] = {
            "supplier_legal_name": s.supplier_legal_name, "supplier_gstin": s.supplier_gstin,
            "supplier_pan": s.supplier_pan, "supplier_state_code": s.supplier_state_code,
            "review_above_amount": f"{s.review_above_amount:.2f}", "auto_issue": s.auto_issue,
            "approver_emails": s.approver_emails if body["can_administer"] else "",
        }
        body["tax_rules"] = [{"id": r.pk, "key": r.key, "label": r.label, "sac_code": r.sac_code,
                              "description": r.description, "gst_rate": f"{r.gst_rate.normalize():f}",
                              "is_default": r.is_default, "illustrative": r.illustrative, "is_active": r.is_active}
                             for r in TaxRule.objects.all()]
        body["counts"] = {st: EInvoice.objects.filter(status=st).count() for st in InvoiceStatus.values}
    return Response(body)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
@_handled
def settings_view(request):
    service.require(service.can_administer, request.user, "Only the Main Administrator edits these settings.")
    s = EinvoiceSettings.get()
    data = request.data
    if "review_above_amount" in data:
        try:
            value = Decimal(str(data["review_above_amount"]))
        except (InvalidOperation, ValueError):
            raise EinvoiceError("Enter an amount.", field="review_above_amount")
        if value < 0:
            raise EinvoiceError("Enter an amount of zero or more.", field="review_above_amount")
        s.review_above_amount = value
    if "auto_issue" in data:
        s.auto_issue = bool(data["auto_issue"]) and str(data["auto_issue"]).lower() not in ("false", "0")
    if "approver_emails" in data:
        s.approver_emails = "\n".join(e.strip() for e in str(data["approver_emails"]).splitlines() if "@" in e)[:4000]
    s.save()
    return Response({"ok": True})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
@_handled
def tax_rule_view(request, rule_id):
    service.require(service.can_administer, request.user, "Only the Main Administrator edits tax rules.")
    rule = TaxRule.objects.filter(pk=rule_id).first()
    if rule is None:
        raise EinvoiceError("Tax rule not found.", status=404, code="not_found")
    data = request.data
    if "sac_code" in data:
        sac = str(data["sac_code"]).strip()
        if not (sac.isdigit() and len(sac) in (4, 6, 8)):
            raise EinvoiceError("A SAC has 4, 6 or 8 digits.", field="sac_code")
        rule.sac_code = sac
    if "gst_rate" in data:
        try:
            rate = Decimal(str(data["gst_rate"]))
        except (InvalidOperation, ValueError):
            raise EinvoiceError("Enter a GST rate.", field="gst_rate")
        if rate not in (Decimal(x) for x in ("0", "5", "12", "18", "28")):
            raise EinvoiceError("GST rates are 0, 5, 12, 18 or 28%.", field="gst_rate")
        rule.gst_rate = rate
    for key in ("label", "description"):
        if key in data and str(data[key]).strip():
            setattr(rule, key, str(data[key]).strip()[:200])
    if "illustrative" in data:
        rule.illustrative = bool(data["illustrative"])
    rule.save()
    return Response({"ok": True})


# --- invoices (staff) -------------------------------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_handled
def invoices_view(request):
    service.require(service.can_review, request.user)
    qs = EInvoice.objects.select_related("customer", "signed_by").order_by("-created_at")
    status_filter = request.query_params.get("status", "")
    if status_filter:
        qs = qs.filter(status__in=[x for x in status_filter.split(",") if x])
    doc_type = request.query_params.get("doc_type", "")
    if doc_type in ("INV", "CRN"):
        qs = qs.filter(doc_type=doc_type)
    supply = request.query_params.get("supply", "")
    if supply in ("B2B", "B2C"):
        qs = qs.filter(supply_type=supply)
    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(invoice_number__icontains=q) | Q(draft_reference__icontains=q) | Q(irn__icontains=q)
                       | Q(customer__legal_name__icontains=q) | Q(customer__gstin__icontains=q)
                       | Q(booking_reference__icontains=q) | Q(receipt_number__icontains=q))
    return Response({"results": [service.invoice_dict(inv) for inv in qs[:300]]})


@transaction.non_atomic_requests
@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_handled
def demo_invoice_view(request):
    inv = service.create_demo(request.data, request.user)
    return Response(service.invoice_dict(_invoice(inv.pk), detail=True), status=201)


@transaction.non_atomic_requests
@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_handled
def from_request_view(request, request_id):
    from iic_booking.external_services.models import SricInvoiceRequest

    service.require(service.can_review, request.user)
    req = SricInvoiceRequest.objects.select_related("booking__user").filter(pk=request_id).first()
    if req is None:
        raise EinvoiceError("Invoice request not found.", status=404, code="not_found")
    inv = service.prepare_from_request(req, request.user)
    return Response(service.invoice_dict(_invoice(inv.pk), detail=True), status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_handled
def invoice_detail_view(request, invoice_id):
    service.require(service.can_review, request.user)
    return Response(service.invoice_dict(_invoice(invoice_id), detail=True))


def _action(fn):
    @transaction.non_atomic_requests
    @api_view(["POST"])
    @permission_classes([IsAuthenticated])
    @_handled
    @wraps(fn)
    def inner(request, invoice_id):
        inv = fn(request, invoice_id)
        return Response(service.invoice_dict(_invoice(inv.pk), detail=True))

    return inner


@_action
def approve_view(request, invoice_id):
    return service.approve(invoice_id, request.user, request.data.get("remarks", ""))


@_action
def reject_view(request, invoice_id):
    return service.reject(invoice_id, request.user, request.data.get("remarks", ""))


@_action
def submit_view(request, invoice_id):
    service.require(service.can_review, request.user)
    return service.submit_irn(invoice_id, request.user)


@_action
def buyer_view(request, invoice_id):
    return service.update_buyer(invoice_id, request.user, request.data)


@_action
def check_gstin_view(request, invoice_id):
    return service.check_gstin(invoice_id, request.user)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_handled
def invoice_pdf_view(request, invoice_id):
    service.require_enabled()
    inv = _invoice(invoice_id)
    owner = inv.booking_id and inv.booking.user_id == request.user.pk
    if not (service.can_review(request.user) or service.is_signatory(request.user) or (owner and inv.issued)):
        raise EinvoiceError("Invoice not found.", status=404, code="not_found")
    out = service.final_pdf(inv)
    if out is None:
        raise EinvoiceError("This invoice has no PDF yet.", status=404, code="no_pdf")
    return _pdf_response(*out)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_handled
def register_view(request):
    service.require(service.can_review, request.user)
    raw = request.query_params.get("month", "")
    try:
        year, month = (int(x) for x in raw.split("-")) if raw else (None, None)
    except ValueError:
        raise EinvoiceError("month must be YYYY-MM.", field="month")
    if year is None:
        from django.utils import timezone

        today = timezone.localdate()
        year, month = today.year, today.month
    if not (1 <= month <= 12):
        raise EinvoiceError("month must be YYYY-MM.", field="month")
    data = service.register(year, month)
    if request.query_params.get("export") == "csv":  # not ?format= (DRF reserves it)
        out = io.StringIO()
        cols = ["invoice_number", "invoice_date", "supply_type", "status_label", "buyer", "buyer_gstin",
                "place_of_supply", "irn", "ack_number", "receipt_number", "booking_reference", "taxable_value", "cgst",
                "sgst", "igst", "total", "signed_by", "simulated"]
        writer = csv.writer(out)
        writer.writerow(["SANDBOX DEMONSTRATION - not for GST returns"])
        writer.writerow(cols)
        for row in data["rows"]:
            writer.writerow([row[c] for c in cols])
        writer.writerow([])
        writer.writerow(["Series", "From", "To", "Count"])
        for s in data["series"]:
            writer.writerow([s["series"], s["from"], s["to"], s["count"]])
        resp = HttpResponse(out.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="gst-invoice-register-{year}-{month:02d}-sandbox.csv"'
        return resp
    return Response(data)


# --- certificates and devices ----------------------------------------------------------------------------------------------

def _cert_dict(c: SignerCertificate) -> dict:
    return {"id": c.pk, "user": {"id": c.user_id, "name": getattr(c.user, "name", ""), "email": c.user.email},
            "label": c.label, "subject": c.subject, "issuer": c.issuer, "serial_number": c.serial_number,
            "fingerprint": c.sha256_fingerprint, "not_before": c.not_before.isoformat(),
            "not_after": c.not_after.isoformat(), "is_test": c.is_test, "is_active": c.is_active,
            "registered_at": c.registered_at.isoformat(), "deactivation_reason": c.deactivation_reason}


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
@_handled
def certificates_view(request):
    if request.method == "GET":
        service.require_enabled()
        if service.can_administer(request.user) or service.can_review(request.user):
            qs = SignerCertificate.objects.select_related("user")
        else:
            qs = SignerCertificate.objects.select_related("user").filter(user=request.user)
        return Response({"results": [_cert_dict(c) for c in qs]})
    service.require(service.can_administer, request.user, "Only the Main Administrator registers certificates.")
    email = (request.data.get("user_email") or "").strip()
    user = get_user_model().objects.filter(email__iexact=email, is_active=True).first() if email else None
    if user is None:
        raise EinvoiceError("No active portal account has that email.", field="user_email")
    upload = request.FILES.get("file")
    if upload is None or upload.size > 64 * 1024:
        raise EinvoiceError("Attach the signatory's certificate (.cer / .crt).", field="file")
    is_test = str(request.data.get("is_test", "")).lower() in ("1", "true", "yes", "on")
    cert = service.register_certificate(request.user, user, upload.read(), label=request.data.get("label", ""),
                                        is_test=is_test)
    return Response(_cert_dict(cert), status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_handled
def certificate_deactivate_view(request, cert_id):
    return Response(_cert_dict(service.deactivate_certificate(request.user, cert_id, request.data.get("reason", ""))))


def _device_dict(d: SignerDevice) -> dict:
    return {"id": d.pk, "name": d.name, "token_hint": d.token_hint + "...", "created_at": d.created_at.isoformat(),
            "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
            "revoked_at": d.revoked_at.isoformat() if d.revoked_at else None}


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@_handled
def devices_view(request):
    service.require(service.is_signatory, request.user, "Only a signatory with a registered DSC certificate links a "
                                                        "signing utility.")
    if request.method == "GET":
        return Response({"results": [_device_dict(d) for d in SignerDevice.objects.filter(user=request.user)]})
    device, token = service.new_device(request.user, request.data.get("name", ""))
    return Response({**_device_dict(device), "token": token,
                     "note": "Copy this token into the signing utility now. It is not shown again."}, status=201)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_handled
def device_revoke_view(request, device_id):
    from django.utils import timezone

    device = SignerDevice.objects.filter(pk=device_id, user=request.user, revoked_at__isnull=True).first()
    if device is None:
        raise EinvoiceError("Device not found.", status=404, code="not_found")
    device.revoked_at = timezone.now()
    device.save(update_fields=["revoked_at"])
    return Response(_device_dict(device))


# --- signing utility API ------------------------------------------------------------------------------------------------

def _signer(fn):
    @transaction.non_atomic_requests
    @api_view(["GET", "POST"])
    @authentication_classes([])
    @permission_classes([AllowAny])
    @parser_classes([MultiPartParser, FormParser])
    @_handled
    @wraps(fn)
    def inner(request, *args, **kwargs):
        service.require_enabled()
        header = request.META.get("HTTP_AUTHORIZATION", "")
        scheme, _, token = header.partition(" ")
        device = service.device_for_token(token.strip()) if scheme == "SricSigner" else None
        if device is None:
            return Response({"error": "Unknown or revoked signing utility token.", "code": "bad_token"}, status=401)
        return fn(request, device, *args, **kwargs)

    return inner


def _queue_item(inv: EInvoice) -> dict:
    return {"id": inv.pk, "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
            "buyer": inv.customer.legal_name, "buyer_state": inv.customer.state_code,
            "taxable_value": f"{inv.taxable_value:.2f}", "tax": f"{inv.cgst + inv.sgst + inv.igst:.2f}",
            "total": f"{inv.total:.2f}", "booking_reference": inv.booking_reference,
            "receipt_number": inv.receipt_number, "pdf_sha256": inv.pdf_sha256,
            "pdf_url": f"/api/v1/gst-einvoice/signer/api/invoices/{inv.pk}/pdf/",
            "upload_url": f"/api/v1/gst-einvoice/signer/api/invoices/{inv.pk}/signed/"}


@_signer
def signer_whoami(request, device):
    certs = SignerCertificate.objects.filter(user=device.user, is_active=True)
    return Response({"device": device.name, "user": getattr(device.user, "name", "") or device.user.email,
                     "mode": conf.mode(), "certificates": [{"subject": c.subject, "fingerprint": c.sha256_fingerprint,
                                                            "not_after": c.not_after.isoformat(), "is_test": c.is_test}
                                                           for c in certs],
                     "signature_box": list(dsc.SIGNATURE_BOX), "signature_field": dsc.SIGNATURE_FIELD})


@_signer
def signer_queue(request, device):
    return Response({"results": [_queue_item(inv) for inv in service.signing_queue()],
                     "signature_box": list(dsc.SIGNATURE_BOX), "signature_field": dsc.SIGNATURE_FIELD})


@_signer
def signer_pdf(request, device, invoice_id):
    inv = EInvoice.objects.filter(pk=invoice_id, status=InvoiceStatus.AWAITING_SIGNATURE).first()
    if inv is None or not inv.pdf:
        raise EinvoiceError("This invoice is not waiting for a signature.", status=404, code="not_found")
    with inv.pdf.open("rb") as fh:
        return _pdf_response(fh.read(), f"tax-invoice-{inv.invoice_number.replace('/', '-')}.pdf")


@_signer
def signer_upload(request, device, invoice_id):
    if request.method != "POST":
        raise EinvoiceError("Upload the signed PDF with POST.", status=405, code="method")
    upload = request.FILES.get("file")
    if upload is None:
        raise EinvoiceError("Attach the signed PDF.", field="file")
    inv = service.accept_signed(invoice_id, device, upload.read())
    return Response({"id": inv.pk, "invoice_number": inv.invoice_number, "status": inv.status,
                     "signed_pdf_sha256": inv.signed_pdf_sha256})


# --- buyer -------------------------------------------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def buyer_booking_view(request, booking_id):
    if not conf.enabled():
        return Response({"enabled": False, "invoice": None})
    return Response({"enabled": True, "invoice": service.buyer_view(request.user, booking_id)})


# --- GSTR-1 export of B2C invoices (reviewers in the portal; the signing utility with its device token) -------------

def _gstr1_response(request):
    from django.utils.dateparse import parse_date

    from . import gstr1

    start, end = parse_date(request.query_params.get("from") or ""), parse_date(request.query_params.get("to") or "")
    if start is None or end is None:
        raise EinvoiceError("Give from and to as YYYY-MM-DD.", field="from")
    kind = (request.query_params.get("kind") or "preview").lower()
    try:
        if kind == "preview":
            return Response({"months": gstr1.preview(start, end), "b2cl_limit": f"{gstr1.B2CL_LIMIT:.2f}",
                             "format": {k: v for k, v in gstr1.FORMAT.items() if k != "excel_sheets"}})
        name, data, ctype = gstr1.export(start, end, kind)
    except ValueError as exc:
        raise EinvoiceError(str(exc), field="to") from exc
    resp = HttpResponse(data, content_type=ctype)
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    resp["Cache-Control"] = "no-store"
    return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_handled
def gstr1_view(request):
    service.require(service.can_review, request.user)
    return _gstr1_response(request)


@_signer
def signer_gstr1(request, device):
    return _gstr1_response(request)


# --- credit notes: the Main Administrator allows one for a single invoice ------------------------------------------

@_action
def credit_note_allow_view(request, invoice_id):
    from . import credit_notes

    return credit_notes.allow(request.user, invoice_id, request.data.get("reason", "")).invoice


@_action
def credit_note_revoke_view(request, invoice_id):
    from . import credit_notes

    return credit_notes.revoke(request.user, invoice_id, request.data.get("reason", "")).invoice

