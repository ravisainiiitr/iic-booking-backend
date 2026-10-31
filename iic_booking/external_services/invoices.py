"""GST invoice workflow for external bookings. The portal never issues the GST invoice: SRIC does.

* Invoice timing preference (append-only): "after analysis" or, where allowed, "before analysis". Choosing the early
  option requires acknowledging the current early-invoice disclosure (version and text hash stored).
* One ``SricInvoiceRequest`` per booking (unique). It is queued:
  - when the user explicitly asks for the early invoice (early preference, payment realised), or
  - by ``queue_completed`` (every 30 minutes) for completed, paid bookings whose preference is "after analysis"
    (or with no preference when ``auto_request_without_preference`` is on).
  A repeated completion event, a double click, or an early request followed by completion all hit the same row.
* The request carries a snapshot of the invoice details from portal records at queue time; nothing is invented.
  Missing billing details stop the request with a message instead of sending a partial one.
* Queued is not sent: ``sric_dispatch`` sends them in the 09:30 working-day batch. Sent is not issued: the invoice
  is "available" only after the Accounts In Charge uploads the document.
"""

from __future__ import annotations

import hashlib
import logging
from decimal import Decimal
from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import notifications, policy
from .models import (
    ExternalServicesSettings,
    InvoiceOption,
    InvoicePreference,
    InvoiceRequestStatus,
    InvoiceTrigger,
    OfficialInvoiceDocument,
    PolicyKey,
    SricInvoiceRequest,
)
from .window import is_external_booking_user

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
CLOSED_BOOKING_STATUSES = ("CANCELLED", "REFUNDED", "BOOKING_NOT_UTILIZED", "ABSENT")


class InvoiceError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", field: str = "", extra=None):
        super().__init__(message)
        self.message, self.status, self.code, self.field = message, status, code, field
        self.extra = extra or {}

    def payload(self) -> dict:
        body = {"error": self.message, "code": self.code}
        if self.field:
            body["field"] = self.field
        body.update(self.extra)
        return body


# --- access ----------------------------------------------------------------------------------------------------------

def _booking_for_owner(user, booking_id):
    from iic_booking.equipment.models import Booking

    try:
        booking = Booking.objects.select_related("equipment", "user").get(pk=int(booking_id))
    except (TypeError, ValueError, Booking.DoesNotExist):
        raise InvoiceError("Booking not found.", status=404, code="not_found")
    if booking.user_id != user.pk:
        raise InvoiceError("Booking not found.", status=404, code="not_found")
    return booking


def is_external_booking(booking) -> bool:
    from iic_booking.users.models.user_type import UserType

    return (booking.user_type_snapshot or "") in UserType.get_external_user_codes() or (
        not booking.user_type_snapshot and is_external_booking_user(booking.user)
    )


def staff_may_handle(user, booking) -> bool:
    """Accounts In Charge (FINANCE) of the booking's department, or the Main Administrator."""
    from iic_booking.department_modules.access import is_main_admin
    from iic_booking.users.models.user_type import UserType

    if is_main_admin(user):
        return True
    if getattr(user, "user_type", None) != UserType.FINANCE or not user.is_active:
        return False
    dept = getattr(booking.equipment, "internal_department_id", None)
    return dept is None or getattr(user, "department_id", None) == dept


def _staff_recipients(booking) -> list:
    """Accounts In Charge of the booking's department; Main Administrators when there are none."""
    from django.contrib.auth import get_user_model

    from iic_booking.users.models.user_type import UserType

    User = get_user_model()
    dept = getattr(booking.equipment, "internal_department_id", None)
    people = list(User.objects.filter(user_type=UserType.FINANCE, is_active=True, department_id=dept)[:10]) if dept \
        else []
    return people or list(User.objects.filter(user_type=UserType.ADMIN, is_active=True)[:5])


def payment_realised(booking) -> bool:
    """Paid in full: confirmed by a settlement path, or nothing was ever due (wallet-paid) and the booking is live."""
    if booking.payment_settled_at:
        return True
    due = Decimal(str(getattr(booking, "amount_due", 0) or 0))
    return due <= 0 and booking.status in ("BOOKED", "PROCESSING", "COMPLETED")


# --- preference ------------------------------------------------------------------------------------------------------

def current_preference(booking) -> Optional[InvoicePreference]:
    return InvoicePreference.objects.filter(booking=booking).order_by("-created_at", "-id").first()


def set_preference(user, booking_id, option, ack_version=None) -> InvoicePreference:
    s = ExternalServicesSettings.load()
    if not s.invoice_requests_enabled:
        raise InvoiceError("Invoice requests are not available yet.", status=403, code="invoices_disabled")
    booking = _booking_for_owner(user, booking_id)
    if not is_external_booking(booking):
        raise InvoiceError("GST invoice requests are for external bookings.", status=403, code="not_external")
    if option not in InvoiceOption.values:
        raise InvoiceError("Choose an invoice option.", field="option")
    ack_policy = None
    if option == InvoiceOption.EARLY:
        if not s.early_invoice_allowed:
            raise InvoiceError("Early GST invoices are not offered at the moment.", status=403,
                               code="early_invoice_disabled")
        try:
            ack_policy = policy.require_acknowledgement(PolicyKey.EARLY_INVOICE, ack_version)
        except policy.PolicyError as exc:
            raise InvoiceError(exc.message, code=exc.code, field=exc.field, extra=exc.extra)
    with transaction.atomic():
        existing = SricInvoiceRequest.objects.filter(booking=booking).first()
        pref = InvoicePreference.objects.create(
            booking=booking, option=option, chosen_by=user, disclosure_policy=ack_policy,
            disclosure_text_sha256=policy.text_sha256(ack_policy.text) if ack_policy else "",
            acknowledged_at=timezone.now() if ack_policy else None,
        )
        # A request already queued or sent keeps its history; the new choice is recorded but does not retract it.
        if existing is not None:
            logger.info("Invoice preference changed after request %s was created (booking %s)", existing.pk, booking.pk)
    return pref


# --- snapshot --------------------------------------------------------------------------------------------------------

def _billing(booking) -> dict:
    from iic_booking.users.models import ExternalBillingProfile

    b = ExternalBillingProfile.objects.filter(user=booking.user).first()
    missing = []
    if b is None:
        missing = ["billing name", "billing address"]
    else:
        for label, value in (("billing name", b.billing_name), ("billing address", b.billing_address_line1),
                             ("city", b.billing_city), ("state", b.billing_state), ("PIN code", b.billing_pincode)):
            if not (value or "").strip():
                missing.append(label)
    if missing:
        raise InvoiceError(
            "Complete your billing details (" + ", ".join(missing) + ") in your profile before an invoice can be "
            "requested.", code="billing_incomplete", field="billing",
        )
    return {
        "billing_name": b.billing_name.strip(),
        "gstin": (b.gstin or "").strip(),
        "address_lines": [x.strip() for x in (b.billing_address_line1, b.billing_address_line2) if (x or "").strip()],
        "city": b.billing_city.strip(),
        "state": b.billing_state.strip(),
        "pincode": b.billing_pincode.strip(),
        "country": (b.billing_country or "India").strip(),
    }


def _payments(booking) -> list[dict]:
    out = []
    try:
        for order in booking.razorpay_payment_orders.filter(status="PAID"):
            payment = order.payments.order_by("-pk").first()
            out.append({
                "method": "Online (Razorpay)",
                "reference": getattr(payment, "razorpay_payment_id", "") or order.razorpay_order_id,
                "amount": f"{order.base_amount:.2f}",
            })
    except Exception:  # noqa: BLE001 - payments app tables are optional in some test setups
        logger.debug("Razorpay orders unavailable for booking %s", booking.pk)
    for receipt in booking.payment_receipts.filter(status="PROCESSED"):
        out.append({"method": "Bank transfer / cash (UTR)", "reference": receipt.utr_reference,
                    "amount": f"{receipt.amount:.2f}"})
    return out


def build_snapshot(booking, *, trigger: str, preference: Optional[InvoicePreference]) -> dict:
    equipment = booking.equipment
    dept = getattr(equipment, "internal_department", None)
    detail = {
        "booking_reference": getattr(booking, "virtual_booking_id", "") or str(booking.pk),
        "booking_id": booking.pk,
        "fbr_number": (getattr(booking, "istem_fbr_number", "") or "").strip(),
        "facility": getattr(dept, "name", "") or "",
        "equipment": {"name": equipment.name, "code": equipment.code},
        "user": {"name": getattr(booking.user, "name", "") or "", "email": booking.user.email,
                 "user_type": booking.user_type_snapshot or booking.user.user_type},
        "billing": _billing(booking),
        "charges": {
            "total": f"{booking.total_charge:.2f}" if booking.total_charge is not None else None,
            "breakdown": booking.charge_breakdown or [],
            "amount_due": f"{Decimal(str(booking.amount_due or 0)):.2f}",
        },
        "payments": _payments(booking),
        "payment_settled_at": timezone.localtime(booking.payment_settled_at).isoformat()
        if booking.payment_settled_at else None,
        "booking_status": booking.status,
        "completed_at": timezone.localtime(booking.completed_at).isoformat() if booking.completed_at else None,
        "trigger": trigger,
        "invoice_timing": preference.option if preference else "",
        "early_acknowledgement": {
            "policy_version": preference.disclosure_policy.version,
            "acknowledged_at": timezone.localtime(preference.acknowledged_at).isoformat(),
            "text_sha256": preference.disclosure_text_sha256,
        } if preference and preference.disclosure_policy_id and preference.acknowledged_at else None,
        "queued_at": timezone.localtime().isoformat(),
    }
    return detail


# --- queueing --------------------------------------------------------------------------------------------------------

def _create_request(booking, *, trigger, preference, actor) -> tuple[SricInvoiceRequest, bool]:
    from .govt_credit import hold_for

    snapshot = build_snapshot(booking, trigger=trigger, preference=preference)
    # Held out of the weekly batch while the user's government credit is open, or until they ask for the consolidated
    # invoice they chose; otherwise queued for the next batch.
    initial_status, group = hold_for(booking)
    try:
        with transaction.atomic():
            req = SricInvoiceRequest.objects.create(
                booking=booking, trigger=trigger, early_requested=trigger == InvoiceTrigger.EARLY,
                preference=preference, requested_by=actor, snapshot=snapshot, status=initial_status,
                consolidation_group=group,
            )
    except IntegrityError:  # one request per booking: the existing one stands
        return SricInvoiceRequest.objects.get(booking=booking), False
    if initial_status == InvoiceRequestStatus.HELD_CREDIT:
        message = ("Your GST invoice request is recorded. As this booking is on credit, it goes to the SRIC office "
                   "once your payment is realised.")
    elif initial_status == InvoiceRequestStatus.HELD_CONSOLIDATION:
        message = ("Your GST invoice request is recorded for your consolidated invoice. Ask for the consolidated "
                   "invoice when you are ready, and it goes to the SRIC office in the next working-day batch.")
    else:
        message = ("Your request for the official GST invoice has been queued. It is sent to the SRIC office in the "
                   "next working-day batch (09:30 IST). SRIC issues the invoice; we will tell you when it is available "
                   "here.")
    notifications.notify(
        f"invoice:{req.pk}:queued", [booking.user],
        title=f"GST invoice request queued for {snapshot['booking_reference']}",
        message=message,
        link=f"/my-bookings?booking={booking.pk}",
    )
    return req, True


def request_early(user, booking_id) -> SricInvoiceRequest:
    s = ExternalServicesSettings.load()
    if not (s.invoice_requests_enabled and s.early_invoice_allowed):
        raise InvoiceError("Early GST invoices are not offered at the moment.", status=403,
                           code="early_invoice_disabled")
    booking = _booking_for_owner(user, booking_id)
    if not is_external_booking(booking):
        raise InvoiceError("GST invoice requests are for external bookings.", status=403, code="not_external")
    if booking.status in CLOSED_BOOKING_STATUSES:
        raise InvoiceError("This booking is closed.", code="booking_closed")
    pref = current_preference(booking)
    if pref is None or pref.option != InvoiceOption.EARLY or not pref.acknowledged_at:
        raise InvoiceError("Choose the early-invoice option and accept its conditions first.",
                           code="early_preference_required")
    from .govt_credit import open_credit

    if not payment_realised(booking) or open_credit(booking.user) is not None:
        # A booking drawn from an open government credit is not paid until the credit is settled.
        raise InvoiceError("The invoice can be requested once your payment is realised.", code="payment_pending")
    req, _created = _create_request(booking, trigger=InvoiceTrigger.EARLY, preference=pref, actor=user)
    return req


def queue_completed(*, limit: int = 300) -> dict:
    """Queue requests for completed, paid external bookings (idempotent; safe to run repeatedly)."""
    from datetime import timedelta

    from iic_booking.equipment.models import Booking
    from iic_booking.users.models.user_type import UserType

    s = ExternalServicesSettings.load()
    if not s.invoice_requests_enabled:
        return {"skipped": "disabled"}
    since = timezone.now() - timedelta(days=60)
    candidates = (
        Booking.objects.filter(status="COMPLETED", completed_at__gte=since,
                               user_type_snapshot__in=list(UserType.get_external_user_codes()))
        .filter(sric_invoice_request__isnull=True)
        .select_related("equipment", "user")
        .order_by("completed_at")[:limit]
    )
    counts = {"queued": 0, "skipped": 0, "incomplete": 0}
    for booking in candidates:
        if not is_external_booking_user(booking.user):  # piloted users only (see pilot.py)
            counts["skipped"] += 1
            continue
        pref = current_preference(booking)
        wants = (pref is not None and pref.option == InvoiceOption.AFTER_ANALYSIS) or (
            pref is None and s.auto_request_without_preference
        )
        if not wants or not payment_realised(booking):
            counts["skipped"] += 1
            continue
        try:
            _req, created = _create_request(booking, trigger=InvoiceTrigger.COMPLETION, preference=pref, actor=None)
        except InvoiceError:
            counts["incomplete"] += 1  # e.g. billing details missing; retried on the next run
            continue
        counts["queued" if created else "skipped"] += 1
    return counts


# --- status for the user ------------------------------------------------------------------------------------------------

def status_for_user(user, booking_id) -> dict:
    booking = _booking_for_owner(user, booking_id)
    s = ExternalServicesSettings.load()
    pref = current_preference(booking)
    req = SricInvoiceRequest.objects.filter(booking=booking).first()
    doc = req.documents.filter(is_current=True).first() if req else None
    if doc is not None:
        stage, next_step = "invoice_available", "Download the official GST invoice."
    elif req is None:
        stage = "waiting_for_analysis" if booking.status != "COMPLETED" else "not_requested"
        next_step = (
            "The invoice request is sent automatically after your analysis is completed."
            if pref and pref.option == InvoiceOption.AFTER_ANALYSIS
            else "Choose when you want the GST invoice."
        )
    elif req.status in (InvoiceRequestStatus.QUEUED, InvoiceRequestStatus.BATCHED):
        stage, next_step = "request_queued", "It is sent to SRIC in the next working-day batch (09:30 IST)."
    elif req.status == InvoiceRequestStatus.SENT:
        stage, next_step = "with_sric", "SRIC is preparing the invoice; it appears here once IIC receives it."
    elif req.status in (InvoiceRequestStatus.FAILED, InvoiceRequestStatus.SENDING):
        stage, next_step = "request_queued", "Sending to SRIC is being retried."
    else:
        stage, next_step = "action_required", "The accounts office is resolving a problem with this request."
    policy_row = policy.current(PolicyKey.EARLY_INVOICE)
    return {
        "booking_id": booking.pk,
        "enabled": s.invoice_requests_enabled,
        "is_external": is_external_booking(booking),
        "early_allowed": s.early_invoice_allowed,
        "early_policy": policy.policy_dict(policy_row) if s.early_invoice_allowed else None,
        "preference": {
            "option": pref.option, "label": pref.get_option_display(),
            "chosen_at": timezone.localtime(pref.created_at).isoformat(),
            "acknowledged_at": timezone.localtime(pref.acknowledged_at).isoformat() if pref.acknowledged_at else None,
        } if pref else None,
        "payment_realised": payment_realised(booking),
        "request": {
            "id": req.pk, "status": req.status, "status_label": req.get_status_display(), "trigger": req.trigger,
            "queued_at": timezone.localtime(req.created_at).isoformat(),
            "sent_at": timezone.localtime(req.sent_at).isoformat() if req.sent_at else None,
            "updated_at": timezone.localtime(req.updated_at).isoformat(),
        } if req else None,
        "document": {
            "id": doc.pk, "invoice_number": doc.invoice_number,
            "uploaded_at": timezone.localtime(doc.uploaded_at).isoformat(),
            "download_url": f"/api/v1/external-booking/invoices/documents/{doc.pk}/download/",
        } if doc else None,
        "stage": stage,
        "next_step": next_step,
        "provisional_invoice_url": f"/api/v1/external-booking/bookings/{booking.pk}/provisional-invoice/",
    }


# --- upload and download ----------------------------------------------------------------------------------------------

def _staff_request(user, request_id, *, lock=False) -> SricInvoiceRequest:
    qs = SricInvoiceRequest.objects.select_related("booking__equipment", "booking__user")
    if lock:
        qs = qs.select_for_update(of=("self",))
    try:
        req = qs.get(pk=int(request_id))
    except (TypeError, ValueError, SricInvoiceRequest.DoesNotExist):
        raise InvoiceError("Invoice request not found.", status=404, code="not_found")
    if not staff_may_handle(user, req.booking):
        raise InvoiceError("Invoice request not found.", status=404, code="not_found")
    return req


def _validate_pdf(upload) -> tuple[bytes, str]:
    if upload is None:
        raise InvoiceError("Attach the invoice PDF.", field="file")
    size = getattr(upload, "size", 0) or 0
    if size <= 0 or size > MAX_UPLOAD_BYTES:
        raise InvoiceError("The file must be a PDF of at most 10 MB.", field="file")
    name = (getattr(upload, "name", "") or "invoice.pdf").replace("\\", "/").split("/")[-1][:200]
    if not name.lower().endswith(".pdf"):
        raise InvoiceError("Only PDF invoices can be uploaded.", field="file")
    data = upload.read()
    if not data.startswith(b"%PDF-"):
        raise InvoiceError("The file is not a valid PDF.", field="file")
    return data, name


def upload_document(user, request_id, upload, *, booking_reference: str, invoice_number: str = "",
                    replace: bool = False, replacement_reason: str = "") -> OfficialInvoiceDocument:
    from django.core.files.base import ContentFile

    _staff_request(user, request_id)  # authorise before reading or validating anything the caller sent
    data, name = _validate_pdf(upload)
    reference = (booking_reference or "").strip()
    with transaction.atomic():
        req = _staff_request(user, request_id, lock=True)
        booking = req.booking
        valid_refs = {str(booking.pk), (getattr(booking, "virtual_booking_id", "") or "").strip().upper(),
                      (getattr(booking, "istem_fbr_number", "") or "").strip().upper()} - {""}
        if reference.upper() not in valid_refs:
            raise InvoiceError("The booking reference or FBR number does not match this request.",
                               field="booking_reference", code="reference_mismatch")
        if req.status not in (InvoiceRequestStatus.SENT, InvoiceRequestStatus.INVOICE_RECEIVED,
                              InvoiceRequestStatus.UNCERTAIN, InvoiceRequestStatus.GAVE_UP):
            raise InvoiceError("The invoice request has not been sent to SRIC yet.", code="not_sent")
        current = req.documents.select_for_update().filter(is_current=True).first()
        if current is not None and not replace:
            raise InvoiceError("An invoice is already on file. Replace it with a reason if it was wrong.",
                               code="already_uploaded")
        if replace:
            if current is None:
                raise InvoiceError("There is no invoice to replace.", code="nothing_to_replace")
            if len((replacement_reason or "").strip()) < 10:
                raise InvoiceError("Give a reason of at least 10 characters for the replacement.",
                                   field="replacement_reason")
            current.is_current = False
            current.save(update_fields=["is_current"])
        doc = OfficialInvoiceDocument(
            request=req, original_name=name, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
            invoice_number=(invoice_number or "").strip()[:64], uploaded_by=user,
            replaces=current if replace else None, replacement_reason=(replacement_reason or "").strip()[:2000],
        )
        doc.file.save(f"{req.pk}-{doc.sha256[:16]}.pdf", ContentFile(data), save=False)
        doc.save()
        req.status = InvoiceRequestStatus.INVOICE_RECEIVED
        req.save(update_fields=["status", "updated_at"])
        from .logistics import release_waiting_copies

        release_waiting_copies(req)
        ref = req.snapshot.get("booking_reference") or booking.pk
        notifications.notify(
            f"invoice:{req.pk}:document:{doc.pk}", [booking.user],
            title=f"Official GST invoice available for {ref}" if not replace else f"Corrected GST invoice for {ref}",
            message="The official GST invoice issued by SRIC is now available to download in the portal.",
            link=f"/my-bookings?booking={booking.pk}",
        )
    return doc


def document_for_download(user, document_id) -> OfficialInvoiceDocument:
    try:
        doc = OfficialInvoiceDocument.objects.select_related("request__booking__equipment").get(pk=int(document_id))
    except (TypeError, ValueError, OfficialInvoiceDocument.DoesNotExist):
        raise InvoiceError("Document not found.", status=404, code="not_found")
    booking = doc.request.booking
    if booking.user_id == user.pk:
        if not doc.is_current:
            raise InvoiceError("Document not found.", status=404, code="not_found")
        return doc
    if staff_may_handle(user, booking):
        return doc
    raise InvoiceError("Document not found.", status=404, code="not_found")


def staff_queue(user, *, status_filter: str = "", q: str = "") -> list[dict]:
    from django.db.models import Q

    from iic_booking.department_modules.access import is_main_admin
    from iic_booking.users.models.user_type import UserType

    if not (is_main_admin(user) or getattr(user, "user_type", None) == UserType.FINANCE):
        raise InvoiceError("Only the Accounts In Charge can see invoice requests.", status=403, code="forbidden")
    qs = SricInvoiceRequest.objects.select_related("booking__equipment", "booking__user").order_by("-created_at")
    if not is_main_admin(user):
        qs = qs.filter(Q(booking__equipment__internal_department_id=user.department_id)
                       | Q(booking__equipment__internal_department__isnull=True))
    if status_filter:
        qs = qs.filter(status__in=[s.strip() for s in status_filter.split(",") if s.strip()])
    if q:
        term = q.strip()
        qs = qs.filter(Q(booking__virtual_booking_id__icontains=term) | Q(booking__istem_fbr_number__icontains=term)
                       | Q(booking__user__email__icontains=term) | Q(booking__user__name__icontains=term))
    out = []
    for req in qs[:300]:
        doc = req.documents.filter(is_current=True).first()
        out.append({
            "id": req.pk, "booking_id": req.booking_id, "status": req.status, "status_label": req.get_status_display(),
            "trigger": req.trigger, "early_requested": req.early_requested,
            "booking_reference": req.snapshot.get("booking_reference"), "fbr_number": req.snapshot.get("fbr_number"),
            "billing_name": (req.snapshot.get("billing") or {}).get("billing_name"),
            "user_email": req.booking.user.email, "equipment": req.booking.equipment.name,
            "queued_at": timezone.localtime(req.created_at).isoformat(),
            "sent_at": timezone.localtime(req.sent_at).isoformat() if req.sent_at else None,
            "attempts": req.attempts, "last_error": req.last_error,
            "document": {"id": doc.pk, "uploaded_at": timezone.localtime(doc.uploaded_at).isoformat()} if doc else None,
            "snapshot": req.snapshot,
        })
    return out
