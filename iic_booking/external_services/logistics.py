"""Saved postal addresses, physical invoice copies by Speed Post, and QR-coded sample labels with staff receipt.

Access rules (all server-side):
* Addresses: only their owner; another user's id is answered with 404.
* Copy requests: the booking's owner asks; the Accounts In Charge of the department (or the Main Administrator)
  prepares and dispatches. The address is copied into the request, so editing the address book later does not
  change a dispatch.
* Sample labels: the booking's (external) owner creates one; each new label revokes the previous ones. The QR code
  holds a random 128-bit token (only its SHA-256 is stored).
* Receipt by scanning (``scan``): opening the QR link is the action; nobody types a code. The step depends on who
  scans and where the sample is, checked under a lock on the booking:
  1. The Accounts In Charge of the booking's department, with nothing pending on the booking: Received at Office
     (status Forwarded to Lab). With an amount pending: nothing is recorded; they may hold it at the office.
  2. Then the equipment's Officer In Charge or lab in-charge: Sample Received at Lab (status Sample Accepted).
  3. Any later scan by them (or by the Main Administrator) shows the booking details only.
  Both steps go through the existing sample-status endpoint as the scanning user, so its rules and side effects
  (external order of steps, results folder) apply unchanged. Anyone else gets "not found".
* Courier details by scanning (``courier_status`` / ``courier_submit``): before office receipt, whoever opens the QR
  link without being receiving staff (normally the sender, from a phone camera, signed in or not) may give the
  courier and tracking number once. They are recorded for the booking's owner exactly as the portal records them;
  after that, or once the portal holds courier details, the label only shows them and changes go through the portal.
  The owner is emailed. Nothing personal is shown to an anonymous scanner.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import notifications
from .models import (
    CopyRequestEvent,
    CopyRequestStatus,
    ExternalServicesSettings,
    PhysicalCopyRequest,
    SampleReceiptScan,
    SampleShipmentLabel,
    SavedPostalAddress,
    SricInvoiceRequest,
)

PIN_RE = re.compile(r"^[1-9][0-9]{5}$")
PHONE_RE = re.compile(r"^[0-9+\-\s()]{6,20}$")
REFERENCE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
RECEIVED_STATUSES = ("HELD_AT_OFFICE", "FORWARDED_TO_LAB", "SAMPLE_ACCEPTED", "SAMPLE_REJECTED", "PROCESSING",
                     "COMPLETED", "RETURNED")
LABEL_OPEN_BOOKING_STATUSES = ("PENDING", "PENDING_PAYMENT", "BOOKED", "HOLD")
COURIER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .&()'-]{1,79}$")


class LogisticsError(Exception):
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


# --- addresses -------------------------------------------------------------------------------------------------------

ADDRESS_FIELDS = {
    "label": (60, False), "recipient_name": (120, True), "organisation": (160, False), "line1": (200, True),
    "line2": (200, False), "city": (100, True), "district": (100, False), "state": (100, True),
    "postal_code": (12, True), "country": (60, False), "phone": (20, False),
}


def clean_address(data: dict) -> dict:
    out = {}
    for field, (limit, required) in ADDRESS_FIELDS.items():
        value = str(data.get(field) or "").strip()
        if required and not value:
            raise LogisticsError(f"Fill in the {field.replace('_', ' ')}.", field=field)
        if len(value) > limit:
            raise LogisticsError(f"Keep the {field.replace('_', ' ')} under {limit} characters.", field=field)
        out[field] = value
    out["country"] = out["country"] or "India"
    if out["country"].lower() == "india" and not PIN_RE.match(out["postal_code"]):
        raise LogisticsError("An Indian PIN code has 6 digits and does not start with 0.", field="postal_code")
    if out["phone"] and not PHONE_RE.match(out["phone"]):
        raise LogisticsError("Enter a valid phone number.", field="phone")
    return out


def address_dict(a: SavedPostalAddress) -> dict:
    return {"id": a.pk, **{f: getattr(a, f) for f in ADDRESS_FIELDS}, "is_default": a.is_default}


def _snapshot(a: SavedPostalAddress) -> dict:
    return {"source_address_id": a.pk, **{f: getattr(a, f) for f in ADDRESS_FIELDS if f != "label"}}


def my_addresses(user) -> list[dict]:
    return [address_dict(a) for a in SavedPostalAddress.objects.filter(user=user, archived_at__isnull=True)]


def _own_address(user, address_id, *, lock=False) -> SavedPostalAddress:
    qs = SavedPostalAddress.objects.filter(user=user, archived_at__isnull=True)
    if lock:
        qs = qs.select_for_update()
    try:
        return qs.get(pk=int(address_id))
    except (TypeError, ValueError, SavedPostalAddress.DoesNotExist):
        raise LogisticsError("Address not found.", status=404, code="not_found")


def save_address(user, data: dict, *, address_id=None) -> SavedPostalAddress:
    """Create, or update (a partial update merged onto the stored address). Ownership is checked first."""
    make_default = str(data.get("is_default", "")).lower() in ("true", "1", "yes", "on") or data.get("is_default") is True
    with transaction.atomic():
        if address_id is None:
            values = clean_address(data)
            if SavedPostalAddress.objects.filter(user=user, archived_at__isnull=True).count() >= 20:
                raise LogisticsError("You can keep at most 20 addresses.", code="too_many")
            address = SavedPostalAddress(user=user, **values)
        else:
            address = _own_address(user, address_id, lock=True)
            merged = {f: getattr(address, f) for f in ADDRESS_FIELDS} | {k: v for k, v in data.items()
                                                                         if k in ADDRESS_FIELDS}
            for k, v in clean_address(merged).items():
                setattr(address, k, v)
        if make_default:
            SavedPostalAddress.objects.filter(user=user, is_default=True).exclude(pk=address.pk).update(is_default=False)
            address.is_default = True
        address.save()
    return address


def remove_address(user, address_id) -> str:
    """Delete an unused address; archive one already used for a dispatch (its snapshot is kept there anyway)."""
    with transaction.atomic():
        address = _own_address(user, address_id, lock=True)
        used = PhysicalCopyRequest.objects.filter(address_snapshot__source_address_id=address.pk).exists()
        if used:
            address.archived_at, address.is_default = timezone.now(), False
            address.save(update_fields=["archived_at", "is_default", "updated_at"])
            return "archived"
        address.delete()
        return "deleted"


# --- physical invoice copies --------------------------------------------------------------------------------------------

def _copy_event(copy, action, actor=None, detail=""):
    CopyRequestEvent.objects.create(copy_request=copy, action=action, actor=actor, detail=(detail or "")[:255])


def copy_dict(copy: PhysicalCopyRequest, *, staff: bool = False) -> dict:
    def iso(v):
        return timezone.localtime(v).isoformat() if v else None

    out = {
        "id": copy.pk,
        "invoice_request_id": copy.invoice_request_id,
        "booking_id": copy.invoice_request.booking_id,
        "booking_reference": copy.invoice_request.snapshot.get("booking_reference"),
        "status": copy.status,
        "status_label": copy.get_status_display(),
        "address": copy.address_snapshot,
        "requested_at": iso(copy.created_at),
        "prepared_at": iso(copy.prepared_at),
        "dispatched_at": iso(copy.dispatched_at),
        "dispatch_date": copy.dispatch_date.isoformat() if copy.dispatch_date else None,
        "speed_post_reference": copy.speed_post_reference,
    }
    if staff:
        out["fbr_number"] = copy.invoice_request.snapshot.get("fbr_number")
        out["events"] = [{"action": e.action, "detail": e.detail, "at": iso(e.created_at)} for e in copy.events.all()]
    return out


def request_copy(user, booking_id, data: dict) -> PhysicalCopyRequest:
    from .invoices import _booking_for_owner, _staff_recipients

    if not ExternalServicesSettings.load().physical_copies_enabled:
        raise LogisticsError("Printed invoice copies are not available at the moment.", status=403,
                             code="copies_disabled")
    booking = _booking_for_owner(user, booking_id)
    req = SricInvoiceRequest.objects.filter(booking=booking).first()
    if req is None:
        raise LogisticsError("Request the GST invoice first; a printed copy can be asked for after that.",
                             code="no_invoice_request")
    if data.get("address_id"):
        address = _own_address(user, data["address_id"])
    else:
        address = save_address(user, data.get("address") or {}) if data.get("save_address") else None
        if address is None:
            values = clean_address(data.get("address") or {})
            snapshot = {"source_address_id": None, **{k: v for k, v in values.items() if k != "label"}}
    if address is not None:
        snapshot = _snapshot(address)
    has_invoice = req.documents.filter(is_current=True).exists()
    try:
        with transaction.atomic():
            copy = PhysicalCopyRequest.objects.create(
                invoice_request=req, requested_by=user, address_snapshot=snapshot,
                status=CopyRequestStatus.REQUESTED if has_invoice else CopyRequestStatus.WAITING_FOR_INVOICE,
            )
            _copy_event(copy, "requested", user)
    except IntegrityError:
        raise LogisticsError("A printed copy is already requested for this invoice.", code="already_requested")
    if has_invoice:
        _notify_accounts_of_copy(copy, _staff_recipients(booking))
    return copy


def _notify_accounts_of_copy(copy, recipients) -> None:
    notifications.notify(
        f"copy:{copy.pk}:requested", recipients,
        title=f"Printed GST invoice requested for {copy.invoice_request.snapshot.get('booking_reference')}",
        message="A user asked for a printed copy of the official GST invoice by Speed Post. Prepare the envelope "
                "from the dispatch queue.",
        link="/accounts/invoice-dispatch",
    )


def release_waiting_copies(invoice_request) -> None:
    """Called after an invoice is uploaded: copies waiting for it become ready for dispatch."""
    from .invoices import _staff_recipients

    for copy in PhysicalCopyRequest.objects.filter(invoice_request=invoice_request,
                                                   status=CopyRequestStatus.WAITING_FOR_INVOICE):
        copy.status = CopyRequestStatus.REQUESTED
        copy.save(update_fields=["status", "updated_at"])
        _copy_event(copy, "invoice_available")
        _notify_accounts_of_copy(copy, _staff_recipients(invoice_request.booking))


def my_copies(user) -> list[dict]:
    qs = PhysicalCopyRequest.objects.filter(requested_by=user).select_related("invoice_request")
    return [copy_dict(c) for c in qs[:100]]


def cancel_copy(user, copy_id) -> PhysicalCopyRequest:
    with transaction.atomic():
        copy = PhysicalCopyRequest.objects.select_for_update().filter(pk=copy_id, requested_by=user).first()
        if copy is None:
            raise LogisticsError("Request not found.", status=404, code="not_found")
        if copy.status not in (CopyRequestStatus.WAITING_FOR_INVOICE, CopyRequestStatus.REQUESTED):
            raise LogisticsError("This copy has already been prepared or dispatched.", code="invalid_state")
        copy.status = CopyRequestStatus.CANCELLED
        copy.save(update_fields=["status", "updated_at"])
        _copy_event(copy, "cancelled", user)
    return copy


def _staff_copy(user, copy_id, *, lock=False) -> PhysicalCopyRequest:
    from .invoices import staff_may_handle

    qs = PhysicalCopyRequest.objects.select_related("invoice_request__booking__equipment")
    if lock:
        qs = qs.select_for_update(of=("self",))
    copy = qs.filter(pk=copy_id).first()
    if copy is None or not staff_may_handle(user, copy.invoice_request.booking):
        raise LogisticsError("Request not found.", status=404, code="not_found")
    return copy


def staff_copy_queue(user, *, status_filter: str = "") -> list[dict]:
    from django.db.models import Q

    from iic_booking.department_modules.access import is_main_admin
    from iic_booking.users.models.user_type import UserType

    if not (is_main_admin(user) or getattr(user, "user_type", None) == UserType.FINANCE):
        raise LogisticsError("Only the Accounts In Charge can see the dispatch queue.", status=403, code="forbidden")
    qs = PhysicalCopyRequest.objects.select_related("invoice_request__booking__equipment").order_by("created_at")
    if not is_main_admin(user):
        qs = qs.filter(Q(invoice_request__booking__equipment__internal_department_id=user.department_id)
                       | Q(invoice_request__booking__equipment__internal_department__isnull=True))
    if status_filter:
        qs = qs.filter(status__in=[s.strip() for s in status_filter.split(",") if s.strip()])
    return [copy_dict(c, staff=True) for c in qs[:300]]


def mark_prepared(user, copy_id) -> PhysicalCopyRequest:
    with transaction.atomic():
        copy = _staff_copy(user, copy_id, lock=True)
        if copy.status != CopyRequestStatus.REQUESTED:
            raise LogisticsError("Only a requested copy can be marked prepared.", code="invalid_state")
        copy.status, copy.prepared_by, copy.prepared_at = CopyRequestStatus.PREPARED, user, timezone.now()
        copy.save(update_fields=["status", "prepared_by", "prepared_at", "updated_at"])
        _copy_event(copy, "prepared", user)
    return copy


def mark_dispatched(user, copy_id, *, dispatch_date, reference: str = "", notes: str = "") -> PhysicalCopyRequest:
    from django.utils.dateparse import parse_date

    _staff_copy(user, copy_id)  # authorise before validating the caller's input
    d = parse_date(str(dispatch_date or ""))
    if d is None:
        raise LogisticsError("Give the dispatch date.", field="dispatch_date")
    if d > timezone.localdate():
        raise LogisticsError("The dispatch date cannot be in the future.", field="dispatch_date")
    reference = (reference or "").strip()[:40]
    with transaction.atomic():
        copy = _staff_copy(user, copy_id, lock=True)
        if copy.status not in (CopyRequestStatus.REQUESTED, CopyRequestStatus.PREPARED):
            raise LogisticsError("This copy cannot be marked dispatched.", code="invalid_state")
        copy.status = CopyRequestStatus.DISPATCHED
        copy.dispatched_by, copy.dispatched_at, copy.dispatch_date = user, timezone.now(), d
        copy.speed_post_reference, copy.notes = reference, (notes or "").strip()[:2000]
        copy.save(update_fields=["status", "dispatched_by", "dispatched_at", "dispatch_date",
                                 "speed_post_reference", "notes", "updated_at"])
        _copy_event(copy, "dispatched", user, f"{d.isoformat()} {reference}".strip())
        notifications.notify(
            f"copy:{copy.pk}:dispatched", [copy.requested_by],
            title="Printed GST invoice dispatched",
            message=f"The printed copy of your GST invoice was sent by Speed Post on {d:%d %b %Y}"
                    + (f" (tracking {reference})." if reference else "."),
            link=f"/my-bookings?booking={copy.invoice_request.booking_id}",
        )
    return copy


def envelope_pdf(user, copy_id) -> bytes:
    from .documents import address_lines, envelope_label_pdf

    copy = _staff_copy(user, copy_id)
    snap = copy.invoice_request.snapshot
    ref = " · ".join(x for x in [snap.get("booking_reference"), snap.get("fbr_number")] if x)
    return envelope_label_pdf(recipient_lines=address_lines(copy.address_snapshot), reference=ref)


# --- sample labels and receipt -----------------------------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_reference() -> str:
    for _ in range(10):
        ref = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(8))
        if not SampleShipmentLabel.objects.filter(reference=ref).exists():
            return ref
    raise LogisticsError("Please try again.", status=503, code="busy")


def create_sample_label(user, booking_id, data: dict) -> tuple[SampleShipmentLabel, bytes]:
    from iic_booking.communication.utils import get_frontend_absolute_url
    from iic_booking.equipment.reports import get_equipment_oic_users
    from iic_booking.users.display import get_user_display_name

    from .documents import address_lines, sample_label_pdf
    from .invoices import _booking_for_owner, is_external_booking

    if not ExternalServicesSettings.load().sample_labels_enabled:
        raise LogisticsError("Sample labels are not available at the moment.", status=403, code="labels_disabled")
    booking = _booking_for_owner(user, booking_id)
    if not is_external_booking(booking):
        raise LogisticsError("Shipment labels are for external bookings.", status=403, code="not_external")
    if booking.status not in LABEL_OPEN_BOOKING_STATUSES:
        raise LogisticsError("This booking is no longer waiting for a sample.", code="booking_closed")
    try:
        count = int(data.get("sample_count") or 1)
    except (TypeError, ValueError):
        count = 0
    if not 1 <= count <= 500:
        raise LogisticsError("Give the number of samples (1 to 500).", field="sample_count")
    description = str(data.get("description") or "").strip()[:200]
    if data.get("address_id"):
        sender = _snapshot(_own_address(user, data["address_id"]))
    else:
        values = clean_address(data.get("sender") or {})
        sender = {"source_address_id": None, **{k: v for k, v in values.items() if k != "label"}}
    token = secrets.token_urlsafe(16)  # 128 bits
    with transaction.atomic():
        SampleShipmentLabel.objects.filter(booking=booking, revoked_at__isnull=True).update(revoked_at=timezone.now())
        label = SampleShipmentLabel.objects.create(
            booking=booking, token_sha256=_hash(token), reference=_new_reference(), sample_count=count,
            description=description, sender_snapshot=sender, created_by=user,
        )
    from iic_booking.equipment.reports import get_equipment_lab_incharge_users

    equipment = booking.equipment
    oics = list(get_equipment_oic_users(equipment))
    lab_staff = list(get_equipment_lab_incharge_users(equipment))
    department = getattr(equipment, "internal_department", None)
    pdf = sample_label_pdf(
        sender_lines=address_lines(sender),
        lab_name=getattr(department, "name", "") or "",
        lab_location=(equipment.location or "").strip().splitlines()[0] if (equipment.location or "").strip() else "",
        equipment_name=equipment.name,
        equipment_code=getattr(equipment, "code", "") or "",
        booking_reference=getattr(booking, "virtual_booking_id", "") or str(booking.pk),
        reference=label.reference,
        qr_url=get_frontend_absolute_url(f"/staff/sample-receipt?code={token}"),
        sample_count=count,
        attention=get_user_display_name(oics[0]) if oics else None,
        lab_incharge=get_user_display_name(lab_staff[0]) if lab_staff else None,
    )
    return label, pdf


def _label_by_token(token: str) -> Optional[SampleShipmentLabel]:
    token = (token or "").strip()
    if not token or len(token) > 64:
        return None
    return (SampleShipmentLabel.objects.select_related("booking__equipment", "booking__user")
            .filter(token_sha256=_hash(token), revoked_at__isnull=True).first())


def staff_can_receive(user, booking) -> bool:
    """The same role and scope rules as the existing sample-status endpoint for the office statuses."""
    from iic_booking.equipment import api_views
    from iic_booking.users.models.user_type import UserType

    if api_views._user_is_accounts_finance_user(user):
        # Accounts In Charge receive only for their own department's bookings.
        return bool(getattr(user, "department_id", None)) and user.department_id == _booking_department_id(booking) \
            and bool(api_views._finance_user_can_access_booking_for_workflow(user, booking))
    if not api_views.check_operator_permission(user):
        return False
    if api_views._oic_booking_scope_denied(user, booking) is not None:
        return False
    if user.user_type == UserType.OPERATOR:
        return booking.equipment_id in set(api_views._get_equipment_ids_for_log_access(user) or [])
    return True


def _receipt_view(label: SampleShipmentLabel) -> dict:
    from iic_booking.users.display import get_user_display_name

    booking = label.booking
    latest = booking.sample_trace_events.order_by("-created_at", "-id").first()
    return {
        "label_reference": label.reference,
        "booking_id": booking.pk,
        "booking_reference": getattr(booking, "virtual_booking_id", "") or booking.pk,
        "booking_status": booking.status,
        "equipment": booking.equipment.name,
        "user_name": get_user_display_name(booking.user),
        "sample_count": label.sample_count,
        "description": label.description,
        "latest_sample_status": latest.status if latest else None,
        "already_received": booking.sample_trace_events.filter(status__in=RECEIVED_STATUSES).exists(),
        "slots": [timezone.localtime(s.start_datetime).isoformat() for s in booking.daily_slots.order_by("start_datetime")],
    }


def staff_lookup(user, token) -> dict:
    label = _label_by_token(token)
    if label is None or not staff_can_receive(user, label.booking):
        raise LogisticsError("Label not found.", status=404, code="not_found")
    SampleReceiptScan.objects.create(label=label, staff=user, action="looked_up")
    return _receipt_view(label)


def staff_confirm(user, token, *, outcome: str, notes: str = "") -> dict:
    from iic_booking.equipment.models import Booking, BookingSampleTrace, SampleTraceStatus

    label = _label_by_token(token)  # authorise before validating the caller's input
    if label is None or not staff_can_receive(user, label.booking):
        raise LogisticsError("Label not found.", status=404, code="not_found")
    statuses = {"forwarded_to_lab": SampleTraceStatus.FORWARDED_TO_LAB,
                "held_at_office": SampleTraceStatus.HELD_AT_OFFICE}
    if outcome not in statuses:
        raise LogisticsError("Choose Forwarded to Lab or Held at Office.", field="outcome")
    notes = (notes or "").strip()[:500]
    if outcome == "held_at_office" and not notes:
        raise LogisticsError("Give the reason the sample is held at the office.", field="notes")
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=label.booking_id)
        if booking.sample_trace_events.filter(status=SampleTraceStatus.RETURNED).exists():
            raise LogisticsError("The sample lifecycle is complete for this booking.", code="lifecycle_complete")
        if booking.sample_trace_events.filter(status__in=RECEIVED_STATUSES).exists():
            SampleReceiptScan.objects.create(label=label, staff=user, action="duplicate", notes=notes)
            return {**_receipt_view(label), "recorded": False, "message": "Receipt was already recorded."}
        BookingSampleTrace.objects.create(
            booking=booking, status=statuses[outcome], reason=notes if outcome == "held_at_office" else "",
            created_by=user,
        )
        SampleReceiptScan.objects.create(label=label, staff=user, action="confirmed", notes=notes,
                                         trace_status=statuses[outcome])
        notifications.notify(
            f"sample:{booking.pk}:received", [booking.user],
            title=f"Sample received for booking {getattr(booking, 'virtual_booking_id', '') or booking.pk}",
            message="The IIC receiving office has received your sample." + (
                " It is being held at the office: " + notes if outcome == "held_at_office" else
                " It has been forwarded to the laboratory."),
            link=f"/my-bookings?booking={booking.pk}",
        )
    return {**_receipt_view(label), "recorded": True}


# --- scan: office receipt, then lab receipt, then details ------------------------------------------------------------

OFFICE_RECEIVED = ("FORWARDED_TO_LAB",)
LAB_RECEIVED = ("SAMPLE_ACCEPTED", "SAMPLE_REJECTED", "PROCESSING", "COMPLETED", "RETURNED")


def _booking_department_id(booking):
    return (getattr(booking.equipment, "internal_department_id", None)
            or getattr(booking, "settlement_department_id", None))


def scan_role(user, booking) -> Optional[str]:
    """``office`` | ``lab`` | ``viewer`` | ``None`` (no access) for this booking's sample."""
    from iic_booking.equipment import api_views
    from iic_booking.equipment.reports import get_equipment_lab_incharge_users, get_equipment_oic_users
    from iic_booking.users.models.user_type import UserType

    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if api_views._user_is_accounts_finance_user(user):
        # Their own department's external bookings, whatever the payment state: an unpaid one is shown as pending
        # (``scan`` records nothing then).
        same_department = bool(getattr(user, "department_id", None)) and (
            user.department_id == _booking_department_id(booking))
        external = (booking.user_type_snapshot or "").strip() in UserType.get_external_user_codes()
        return "office" if same_department and external else None
    lab_people = {u.pk for u in get_equipment_oic_users(booking.equipment)}
    lab_people |= {u.pk for u in get_equipment_lab_incharge_users(booking.equipment)}
    if user.pk in lab_people:
        return "lab"
    if getattr(user, "user_type", None) == UserType.ADMIN:
        return "viewer"
    return None


def _set_sample_status(user, booking, status_value: str) -> tuple[int, dict]:
    """The existing sample-status endpoint, called as ``user`` (same rules and side effects as the booking page)."""
    from iic_booking.equipment.api_views import set_booking_sample_status
    from iic_booking.research_copilot.services.v2.mutations.domain_bridge import _as_data, _django_json_post

    request = _django_json_post(user=user, path=f"/api/bookings/{booking.pk}/sample-status/",
                                body={"status": status_value})
    return _as_data(set_booking_sample_status(request, booking.pk))


def _scan_details(label: SampleShipmentLabel) -> dict:
    """What staff need to sort the parcel and run the experiment (no contact details beyond the user's name)."""
    from iic_booking.equipment.booking_payment_service import booking_payment_fully_settled
    from iic_booking.equipment.input_display import booking_display_values, booking_input_fields, readable_inputs
    from iic_booking.equipment.reports import get_equipment_lab_incharge_users, get_equipment_oic_users
    from iic_booking.users.display import get_user_display_name

    booking = label.booking
    equipment = booking.equipment
    department = getattr(equipment, "internal_department", None)
    try:
        inputs = readable_inputs(booking_display_values(booking), booking_input_fields(booking))
    except Exception:  # noqa: BLE001 - the details are still useful without the inputs table
        inputs = {"sets": 0, "fields": [], "comments": ""}
    timeline = [
        {"status": e.status, "label": e.get_status_display(), "at": timezone.localtime(e.created_at).isoformat(),
         "by": get_user_display_name(e.created_by) if e.created_by_id else "", "reason": e.reason or ""}
        for e in booking.sample_trace_events.select_related("created_by").order_by("created_at", "id")
    ]
    return {
        **_receipt_view(label),
        "lab": getattr(department, "name", "") or "",
        "equipment_code": getattr(equipment, "code", "") or "",
        "location": (equipment.location or "").strip(),
        "officers_in_charge": [get_user_display_name(u) for u in get_equipment_oic_users(equipment)],
        "lab_incharge": [get_user_display_name(u) for u in get_equipment_lab_incharge_users(equipment)],
        "slot_ranges": [
            {"start": timezone.localtime(s.start_datetime).isoformat(),
             "end": timezone.localtime(s.end_datetime).isoformat()}
            for s in booking.daily_slots.order_by("start_datetime")
        ],
        "amount_due": f"{booking.amount_due or 0:.2f}",
        "payment_settled": booking_payment_fully_settled(booking),
        "notes": (booking.notes or "").strip(),
        "inputs": inputs,
        "timeline": timeline,
        "received_at_office": any(e["status"] in OFFICE_RECEIVED + LAB_RECEIVED for e in timeline),
        "received_at_lab": any(e["status"] in LAB_RECEIVED for e in timeline),
    }


def scan(user, token) -> dict:
    """Process a scanned label for ``user``. Returns the details plus ``outcome``:
    ``received_at_office`` | ``received_at_lab`` | ``payment_pending`` | ``awaiting_office`` | ``details``."""
    from iic_booking.equipment.booking_payment_service import booking_payment_fully_settled
    from iic_booking.equipment.models import Booking

    label = _label_by_token(token)
    ref = (token or "").strip().upper()
    if label is None and re.fullmatch(f"[{REFERENCE_ALPHABET}]{{8}}", ref):
        # The short reference printed under the QR code, for a damaged code (same role rules decide access).
        label = (SampleShipmentLabel.objects.select_related("booking__equipment", "booking__user")
                 .filter(reference=ref, revoked_at__isnull=True).first())
    role = scan_role(user, label.booking) if label is not None else None
    if role is None:
        raise LogisticsError("This label was not found, or it is not for you to receive.", status=404,
                             code="not_found")
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=label.booking_id)  # one scan at a time per booking
        statuses = set(booking.sample_trace_events.values_list("status", flat=True))
        office_done = bool(statuses & set(OFFICE_RECEIVED + LAB_RECEIVED))
        lab_done = bool(statuses & set(LAB_RECEIVED))
        reference = getattr(booking, "virtual_booking_id", "") or booking.pk
        outcome, message = "details", ""
        if role == "office" and not office_done:
            if booking.status not in ("BOOKED", "COMPLETED") or not booking_payment_fully_settled(booking):
                outcome = "payment_pending"
                message = (f"Payment of ₹{booking.amount_due or 0:.2f} is pending on this booking. The sample was "
                           "not forwarded; keep it at the office until the payment is settled.")
            else:
                code, data = _set_sample_status(user, booking, "FORWARDED_TO_LAB")
                if code not in (200, 201):
                    raise LogisticsError(str(data.get("error") or "The receipt could not be recorded."),
                                         status=409, code="not_recorded")
                outcome, message = "received_at_office", "Received at office. Hand the sample over to the lab."
                notifications.notify(
                    f"sample:{booking.pk}:office", [booking.user], title=f"Sample received for booking {reference}",
                    message="The IIC receiving office has received your sample and is forwarding it to the lab.",
                    link=f"/my-bookings?booking={booking.pk}",
                )
        elif role == "lab" and not office_done:
            outcome, message = "awaiting_office", "The receiving office has not recorded this sample yet."
        elif role == "lab" and not lab_done:
            code, data = _set_sample_status(user, booking, "SAMPLE_ACCEPTED")
            if code not in (200, 201):
                raise LogisticsError(str(data.get("error") or "The receipt could not be recorded."),
                                     status=409, code="not_recorded")
            outcome, message = "received_at_lab", "Sample received at the lab."
            notifications.notify(
                f"sample:{booking.pk}:lab", [booking.user],
                title=f"Sample received at the lab for booking {reference}",
                message="The laboratory has received your sample.", link=f"/my-bookings?booking={booking.pk}",
            )
        SampleReceiptScan.objects.create(
            label=label, staff=user,
            action={"received_at_office": "office", "received_at_lab": "lab"}.get(outcome, "looked_up"),
            trace_status={"received_at_office": "FORWARDED_TO_LAB", "received_at_lab": "SAMPLE_ACCEPTED"}.get(
                outcome, ""),
        )
    label.refresh_from_db()
    return {**_scan_details(label), "outcome": outcome, "message": message, "role": role}


# --- first scan: courier details from the sender ---------------------------------------------------------------------

def _tracking_tail(number: str) -> str:
    """The last four characters only: enough for the sender to recognise it, not to track the parcel."""
    return f"…{number[-4:]}" if len(number) > 4 else number


def _recorded_courier(booking) -> Optional[dict]:
    """The courier details already held for the booking (from a scan or the portal), or None."""
    from iic_booking.courier_tracking.constants import ArrivalMode
    from iic_booking.courier_tracking.models import SampleArrival
    from iic_booking.equipment.models import SampleTraceStatus

    arrival = SampleArrival.objects.filter(booking=booking, is_active=True, arrival_mode=ArrivalMode.COURIER).first()
    if arrival is not None:
        return {"courier_name": arrival.courier_name, "tracking_tail": _tracking_tail(arrival.tracking_number)}
    sent = booking.sample_trace_events.filter(status=SampleTraceStatus.SAMPLE_SENT).order_by("created_at", "id").first()
    if sent is not None:
        name, _, number = (sent.tracking_id or "").rpartition(":")
        return {"courier_name": name.strip(), "tracking_tail": _tracking_tail(number.strip())}
    return None


def _courier_lock_reason(label: SampleShipmentLabel, booking) -> str:
    """Why courier details can no longer be given through this label ("" when they can)."""
    if label.courier_recorded_at is not None:
        return ("The courier details were already given by scanning this label. They can be changed only on the "
                "portal (My Bookings).")
    if _recorded_courier(booking) is not None:
        return "The courier details are already recorded on the portal. They can be changed only there."
    if booking.sample_trace_events.filter(status__in=RECEIVED_STATUSES).exists():
        return "IIC has already received this sample."
    if booking.status not in LABEL_OPEN_BOOKING_STATUSES:
        return "This booking is no longer waiting for a sample."
    return ""


def _courier_view(label: SampleShipmentLabel, booking) -> dict:
    """What anyone holding the label may see: the label's own printed facts, never the user's contact details."""
    from iic_booking.courier_tracking.eligibility import arrival_eligibility
    from iic_booking.courier_tracking.services import catalog

    equipment = booking.equipment
    department = getattr(equipment, "internal_department", None)
    reason = _courier_lock_reason(label, booking)
    tracked = arrival_eligibility(booking.user, booking).courier_allowed if not reason else False
    view = {
        "label_reference": label.reference,
        "booking_reference": getattr(booking, "virtual_booking_id", "") or booking.pk,
        "equipment": equipment.name,
        "lab": getattr(department, "name", "") or "",
        "sample_count": label.sample_count,
        "can_submit": not reason,
        "reason": reason,
        "recorded": _recorded_courier(booking),
        # With courier tracking on for the department the courier is picked from the provider's list (so the parcel
        # can be followed); otherwise its name is typed.
        "courier_list": tracked,
        "couriers": [],
    }
    if tracked:
        view["couriers"], _ = catalog(refresh_if_stale=False)  # an anonymous page never spends provider calls
    return view


def courier_status(token: str) -> dict:
    label = _label_by_token(token)
    if label is None:
        raise LogisticsError("This label was not found.", status=404, code="not_found")
    return _courier_view(label, label.booking)


def courier_submit(token: str, *, courier_slug: str = "", courier_name: str = "", tracking_number: str = "") -> dict:
    """Record the courier and tracking number given by scanning the label, once, for the booking's owner."""
    from iic_booking.courier_tracking.constants import ArrivalMode
    from iic_booking.courier_tracking.eligibility import arrival_eligibility
    from iic_booking.courier_tracking.parsing import normalize_tracking_number, tracking_number_problem
    from iic_booking.courier_tracking.services import ArrivalError, ArrivalInput, register_arrival
    from iic_booking.equipment.models import Booking, BookingSampleTrace, SampleTraceStatus

    label = _label_by_token(token)
    if label is None:
        raise LogisticsError("This label was not found.", status=404, code="not_found")
    with transaction.atomic():
        booking = Booking.objects.select_for_update(of=("self",)).select_related("equipment", "user").get(pk=label.booking_id)
        label = SampleShipmentLabel.objects.select_for_update().get(pk=label.pk)
        if label.revoked_at is not None:
            raise LogisticsError("This label was replaced by a newer one.", status=404, code="not_found")
        reason = _courier_lock_reason(label, booking)
        if reason:
            raise LogisticsError(reason, status=409, code="courier_locked")
        if arrival_eligibility(booking.user, booking).courier_allowed:
            try:
                arrival = register_arrival(booking.user, booking, ArrivalInput(
                    arrival_mode=ArrivalMode.COURIER, courier_slug=courier_slug, tracking_number=tracking_number,
                    notes="Given by scanning the sample label.", email_updates=True,
                ))
            except ArrivalError as exc:
                raise LogisticsError(exc.message, status=exc.status, code=exc.code, field=exc.field) from exc
            name, number = arrival.courier_name, arrival.tracking_number
        else:
            name = re.sub(r"\s+", " ", courier_name or "").strip()
            if not COURIER_NAME_RE.match(name):
                raise LogisticsError("Enter the courier company's name.", field="courier_name")
            number = normalize_tracking_number((tracking_number or "")[:64])
            problem = tracking_number_problem("", number)
            if problem:
                raise LogisticsError(problem, field="tracking_number")
            BookingSampleTrace.objects.create(booking=booking, status=SampleTraceStatus.SAMPLE_SENT,
                                              tracking_id=f"{name}: {number}", created_by=booking.user)
        label.courier_recorded_at = timezone.now()
        label.save(update_fields=["courier_recorded_at"])
        SampleReceiptScan.objects.create(label=label, staff=None, action="courier", notes=f"{name}: {number}"[:500])
        reference = getattr(booking, "virtual_booking_id", "") or booking.pk
        notifications.notify(
            f"sample:{booking.pk}:courier", [booking.user],
            title=f"Courier details recorded for booking {reference}",
            message=(f"The sample label of booking {reference} was scanned and these courier details were recorded: "
                     f"{name}, tracking number {number}. If they are not right, change them under My Bookings; "
                     "the label cannot be used to change them again."),
            link=f"/my-bookings?booking={booking.pk}",
        )
    return _courier_view(label, booking)
