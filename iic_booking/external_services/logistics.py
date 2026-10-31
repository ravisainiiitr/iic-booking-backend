"""Saved postal addresses, physical invoice copies by Speed Post, and sample shipment labels.

Access rules (all server-side):
* Addresses: only their owner; another user's id is answered with 404.
* Copy requests: the booking's owner asks; the Accounts In Charge of the department (or the Main Administrator)
  prepares and dispatches. The address is copied into the request, so editing the address book later does not
  change a dispatch.
* Sample labels: the booking's (external) owner creates one; each new label revokes the previous ones. Since the
  31 Oct 2026 committee meeting the label has no QR code: the receiving office reads the booking ID and records the
  receipt on the booking as for any other sample.
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


def _keep_sender_address(user, values: dict) -> Optional[SavedPostalAddress]:
    """A new sender address typed for a label goes into the address book (an identical active one is reused), so the
    next label offers it. None when the book is full: the label is still made from the typed address."""
    same = {f: values[f] for f in ADDRESS_FIELDS if f != "label"}
    existing = SavedPostalAddress.objects.filter(user=user, archived_at__isnull=True, **same).first()
    if existing is not None:
        return existing
    if SavedPostalAddress.objects.filter(user=user, archived_at__isnull=True).count() >= 20:
        return None
    return SavedPostalAddress.objects.create(user=user, **values)


def last_label_address_id(user) -> Optional[int]:
    """The saved address used as sender on the user's most recent sample label, while it is still in the book."""
    label = SampleShipmentLabel.objects.filter(created_by=user).order_by("-created_at", "-id").first()
    source = (label.sender_snapshot or {}).get("source_address_id") if label else None
    if source and SavedPostalAddress.objects.filter(pk=source, user=user, archived_at__isnull=True).exists():
        return int(source)
    return None


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
    from iic_booking.equipment.reports import get_equipment_oic_users
    from iic_booking.users.display import get_user_display_name

    from .documents import address_lines, department_office_mobile, sample_label_pdf
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
        kept = _keep_sender_address(user, values)
        sender = _snapshot(kept) if kept else {"source_address_id": None, **{k: v for k, v in values.items() if k != "label"}}
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
        sender_lines=address_lines(sender, phone=False),
        sender_mobile=sender.get("phone", "") or "",
        office_mobile=department_office_mobile(department),
        lab_name=getattr(department, "name", "") or "",
        lab_location=(equipment.location or "").strip().splitlines()[0] if (equipment.location or "").strip() else "",
        equipment_name=equipment.name,
        equipment_code=getattr(equipment, "code", "") or "",
        booking_reference=getattr(booking, "virtual_booking_id", "") or str(booking.pk),
        reference=label.reference,
        sample_count=count,
        attention=get_user_display_name(oics[0]) if oics else None,
        lab_incharge=get_user_display_name(lab_staff[0]) if lab_staff else None,
    )
    return label, pdf


def _booking_department_id(booking):
    return (getattr(booking.equipment, "internal_department_id", None)
            or getattr(booking, "settlement_department_id", None))


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
