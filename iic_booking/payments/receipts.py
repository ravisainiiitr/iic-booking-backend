"""Payment receipt data for an online (Razorpay) payment.

The printed receipt number is IITR/<department>/<year>/<serial> (receipt_numbers.py); the order's Razorpay receipt
reference stays available as ``gateway_receipt``. A receipt is never a GST tax invoice: where a tax
invoice applies, SRIC issues it.
"""

from __future__ import annotations

import re

from django.conf import settings

from .models import Payment
from .models import PaymentOrderStatus
from .models import PaymentPurpose
from .models import PaymentStatus
from .purpose import booking_purpose
from .purpose import profile_type_of
from .purpose import recharge_purpose

RECEIPT_STATUSES = (PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED)


def checkout_name() -> str:
    """The merchant name shown in Razorpay Checkout and on receipts. Must match the Razorpay account's business name."""
    return str(getattr(settings, "RAZORPAY_CHECKOUT_NAME", "") or "DEAN SRIC IIT ROORKEE").strip()


_PLURAL_ONE = re.compile(r"(?<![\d.])1 (sample|slot|hour|part|piece|element|set|unit|item)s\b", re.IGNORECASE)


def _clean_charge_text(text: str) -> str:
    """A charge-breakdown line made fit for the receipt: rupees as Rs., whole numbers without .0, '1 sample'."""
    s = " ".join(str(text or "").split())
    s = s.replace("₹", "Rs. ").replace("Rs.  ", "Rs. ")
    s = re.sub(r"(?<![\d.])(\d+)\.0(?!\d)", r"\1", s)  # 1.0 samples -> 1 samples
    s = _PLURAL_ONE.sub(lambda m: f"1 {m.group(1)}", s)
    s = re.sub(r"@ (?=\d)", "@ Rs. ", s)
    return s[:140]


def charge_lines(booking) -> list[str]:
    """Quantity lines for the receipt from the booking's charge breakdown: samples, slots or hours for analysis;
    weight, parts and machine time for fabrication. GST and zero-amount lines are left out (GST has its own column)."""
    rows = getattr(booking, "charge_breakdown", None) or []
    out: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        desc = str(row.get("description") or "").strip()
        try:
            amount = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if not desc or desc.upper().startswith("GST") or abs(amount) < 0.005:
            continue
        out.append(_clean_charge_text(desc))
    return out[:5]


def _service(order) -> dict:
    booking = order.booking
    checkout = getattr(order, "external_checkout", None) if order.booking_id is None else None
    if booking is None and checkout is not None:
        booking = checkout.booking
    if order.purpose == PaymentPurpose.WALLET_RECHARGE:
        department = getattr(order.department, "name", "") or "department"
        return {"description": recharge_purpose(department), "booking_reference": "", "equipment": "",
                "booking_total": "", "booking_gst": "", "charge_lines": []}
    equipment = getattr(getattr(booking, "equipment", None), "name", "") or getattr(getattr(checkout, "equipment", None), "name", "")
    reference = (getattr(booking, "virtual_booking_id", "") or str(getattr(booking, "booking_id", "") or "")) if booking else ""
    gst = None
    if booking is not None:
        from iic_booking.external_services.refunds import _gst_in_breakdown

        gst = _gst_in_breakdown(booking)
    return {
        "description": booking_purpose(equipment, profile_type_of(booking, getattr(booking, "equipment", None) or getattr(checkout, "equipment", None))),
        "booking_reference": reference,
        "equipment": equipment,
        # The booking's own total and the GST inside it, from its charge breakdown (blank for wallet recharges).
        "booking_total": str(booking.total_charge) if booking is not None and booking.total_charge is not None else "",
        "booking_gst": str(gst) if gst else "",
        # Quantity details for the particulars line (samples, slots, weight, machine time ...).
        "charge_lines": charge_lines(booking) if booking is not None else [],
    }


def institute_gstin() -> str:
    """The SRIC office GSTIN printed on its receipts (shown for reference; this receipt is not a tax invoice)."""
    return template_values()["institute_gstin"]


def _settings_gstin() -> str:
    return str(getattr(settings, "RECEIPT_INSTITUTE_GSTIN", "") or "").strip().upper()


# The receipt's editable wording and look. A blank stored value means the default below.
TEXT_FIELDS = (
    "institution_name", "office_name", "address", "institute_gstin", "contact_email", "website", "receipt_heading",
    "service_name", "not_a_tax_invoice", "refund_note", "extra_note", "computer_generated", "brand_color", "accent_color",
)
FLAG_FIELDS = ("show_gstin", "show_logo", "show_watermark")


def template_defaults() -> dict:
    return {
        "institution_name": "Indian Institute of Technology Roorkee",
        "office_name": "Sponsored Research & Industrial Consultancy (SRIC)",
        "address": "Roorkee – 247667, Uttarakhand, India",
        "institute_gstin": _settings_gstin(),
        "contact_email": "arsric@iitr.ac.in",
        "website": "equip.iitr.ac.in",
        "receipt_heading": "Payment Receipt",
        "service_name": "IIT Roorkee Equipment Booking Portal",
        "not_a_tax_invoice": "This is a payment receipt, not a GST tax invoice. Where a tax invoice applies, it is issued by SRIC, IIT Roorkee.",
        "refund_note": "Refunds, where due, go to the portal wallet or to a bank account through the institute, not automatically back to the card or UPI account used.",
        "extra_note": "",
        "computer_generated": "This is a computer-generated receipt and does not require a signature.",
        "brand_color": "#153F79",
        "accent_color": "#C29938",
        "show_gstin": True,
        "show_logo": True,
        "show_watermark": True,
    }


def _stored_template():
    from .models import ReceiptTemplate

    return ReceiptTemplate.objects.filter(pk=1).first()


def template_values(row=None) -> dict:
    """The wording and look every receipt uses now: stored values, with blanks filled from the defaults."""
    out = template_defaults()
    row = row if row is not None else _stored_template()
    if row is None:
        return out
    for field in TEXT_FIELDS:
        value = (getattr(row, field, "") or "").strip()
        if value:
            out[field] = value
    for field in FLAG_FIELDS:
        out[field] = bool(getattr(row, field, True))
    return out


_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_WEBSITE_RE = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/\S*)?$")
_LIMITS = {"institution_name": 160, "office_name": 160, "address": 240, "contact_email": 120, "website": 120,
           "receipt_heading": 60, "service_name": 120, "not_a_tax_invoice": 400, "refund_note": 400, "extra_note": 400,
           "computer_generated": 160}


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def clean_template_input(data: dict) -> tuple[dict, dict]:
    """Checks an edit from the admin page. Returns (cleaned values for the fields sent, errors by field)."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    cleaned, errors = {}, {}
    for field in TEXT_FIELDS:
        if field not in data:
            continue
        value = data.get(field)
        value = "" if value is None else str(value).strip()
        if field in _LIMITS and len(value) > _LIMITS[field]:
            errors[field] = f"At most {_LIMITS[field]} characters."
            continue
        if field == "institute_gstin" and value:
            value = value.upper()
            if not _GSTIN_RE.match(value):
                errors[field] = "Not a valid GSTIN (15 characters, e.g. 05AAALI0033R1Z5)."
                continue
        if field == "contact_email" and value:
            try:
                validate_email(value)
            except ValidationError:
                errors[field] = "Not a valid email address."
                continue
        if field == "website" and value:
            value = re.sub(r"^https?://", "", value).rstrip("/")
            if not _WEBSITE_RE.match(value):
                errors[field] = "Give a web address such as equip.iitr.ac.in."
                continue
        if field in ("brand_color", "accent_color") and value:
            if not _COLOR_RE.match(value):
                errors[field] = "Give a colour as #RRGGBB."
                continue
            value = value.upper()
            # White text sits on the brand colour (header bands, amount box, total row): it must stay readable.
            if field == "brand_color" and (1.05 / (_luminance(value) + 0.05)) < 4.5:
                errors[field] = "Too light: white text on this colour would be hard to read. Choose a darker colour."
                continue
        cleaned[field] = value
    for field in FLAG_FIELDS:
        if field in data:
            raw = data.get(field)
            cleaned[field] = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    return cleaned, errors


def save_template(cleaned: dict, user) -> dict:
    """Saves an admin edit and records what changed. Returns the same payload as ``template_payload``."""
    from django.db import transaction

    from .models import ReceiptTemplate
    from .models import ReceiptTemplateChange

    with transaction.atomic():
        ReceiptTemplate.objects.get_or_create(pk=1)
        row = ReceiptTemplate.objects.select_for_update().get(pk=1)
        changes = {}
        for field, value in cleaned.items():
            old = getattr(row, field)
            if old != value:
                changes[field] = {"from": old, "to": value}
                setattr(row, field, value)
        if changes:
            row.updated_by = user if getattr(user, "pk", None) else None
            row.save()
            ReceiptTemplateChange.objects.create(changed_by=row.updated_by, changes=changes)
    return template_payload(row)


def _who(user) -> str:
    if user is None:
        return ""
    return getattr(user, "name", "") or getattr(user, "email", "") or ""


def template_payload(row=None) -> dict:
    """What the admin page shows: stored values (blank = default), the defaults, the values in use, and recent changes."""
    from .models import ReceiptTemplateChange

    row = row if row is not None else _stored_template()
    stored = {f: (getattr(row, f, "") if row else "") for f in TEXT_FIELDS}
    stored.update({f: (getattr(row, f, True) if row else True) for f in FLAG_FIELDS})
    history = [
        {"changed_at": c.changed_at.isoformat(), "changed_by": _who(c.changed_by), "fields": sorted(c.changes.keys())}
        for c in ReceiptTemplateChange.objects.select_related("changed_by")[:20]
    ]
    return {
        "stored": stored,
        "defaults": template_defaults(),
        "effective": template_values(row),
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        "updated_by": _who(row.updated_by) if row and row.updated_by_id else "",
        "collected_by": checkout_name(),
        "history": history,
    }


def _payer_organisation(user) -> str:
    for attr in ("organisation", "organization", "institute_name", "company_name"):
        value = getattr(user, attr, "")
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    department = getattr(user, "department", None)
    return (getattr(department, "name", "") or "")[:200]


def department_accounts_email(order) -> str:
    """Email of the Accounts In Charge of the department the money is for; blank when it has none.

    Payment questions go to that department's accounts, not to a central address. With several, the
    longest-serving (lowest id) is printed, so the same receipt always shows the same address.
    """
    from django.contrib.auth import get_user_model

    from iic_booking.users.models.user_type import UserType

    from .receipt_numbers import order_department

    department = order_department(order)
    if department is None:
        return ""
    email = (
        get_user_model().objects.filter(user_type=UserType.FINANCE, is_active=True, department_id=department.pk)
        .exclude(email="")
        .order_by("pk")
        .values_list("email", flat=True)
        .first()
    )
    return (email or "").strip()


def receipt_for(payment: Payment) -> dict:
    from .instrument import describe

    order = payment.payment_order
    user = order.user
    how = describe(payment)
    t = template_values()
    # The department's Accounts In Charge answers payment questions; the template's email is the fallback.
    t["contact_email"] = department_accounts_email(order) or t["contact_email"]
    from .receipt_numbers import order_department

    # The department whose facility the money is for (the recharged department for a wallet recharge), with the
    # SRIC / accounts grant code its income is booked under. Blank when the department has no grant code set.
    department = order_department(order)
    from .receipt_numbers import ensure as ensure_receipt_number

    number = ensure_receipt_number(payment)
    from .receipt_verification import verification_code
    from .receipt_verification import verify_url

    return {
        "receipt_number": number,
        # Anyone holding the receipt can check it on <site>/verify-receipt (QR code on the receipt).
        "verification_code": verification_code(payment),
        "verify_url": verify_url(payment),
        # The reference the order carries at Razorpay (shown to finance; not the printed receipt number).
        "gateway_receipt": order.receipt,
        "title": t["receipt_heading"],
        "not_a_tax_invoice": t["not_a_tax_invoice"],
        "computer_generated": t["computer_generated"],
        "test_mode": order.mode == "test",
        "collected_by": checkout_name(),
        "institution": t["institution_name"],
        # Under the institute's name: the department whose equipment (or sub-wallet) the payment is for; the
        # template's office name only when no department is known.
        "office": (getattr(department, "name", "") or "").strip() or t["office_name"],
        "institute_gstin": t["institute_gstin"] if t["show_gstin"] else "",
        "service_name": t["service_name"],
        # The rest of the template the Main Admin edits on the dashboard (Receipt template).
        "template": {k: t[k] for k in ("address", "contact_email", "website", "refund_note", "extra_note", "brand_color",
                                         "accent_color", "show_gstin", "show_logo", "show_watermark")},
        "payer_name": getattr(user, "name", "") or "",
        "payer_organisation": _payer_organisation(user),
        "payer_email": getattr(user, "email", "") or "",
        "purpose": order.get_purpose_display(),
        **_service(order),
        "department_name": (getattr(department, "name", "") or "").strip(),
        "grant_code": (getattr(department, "internal_grant_code", "") or "").strip(),
        "razorpay_payment_id": payment.razorpay_payment_id,
        "razorpay_order_id": order.razorpay_order_id,
        "method": payment.method,
        # How the payment was made, with that mode's own references (UPI RRN, card authorisation code, bank txn no.).
        "payment_mode": how["mode"],
        "payment_details": how["details"],
        # The bank's own reference for the payment (UPI RRN / netbanking transaction no.) as Razorpay reports it, and the
        # UTR of the bank transfer in which Razorpay settled it to the institute. Blank until known; cards have no UTR.
        "bank_reference": payment.gateway_reference or "",
        "settlement_utr": _settlement_utr(payment),
        "paid_at": payment.created_at.isoformat(),
        "status": payment.get_status_display(),
        "currency": order.currency,
        "base_amount": str(order.base_amount),
        "convenience_fee": str(order.convenience_fee),
        "gst_on_fee": str(order.fee_gst),
        "gst_on_fee_percent": str(order.fee_gst_percent_snapshot),
        "total_paid": str(payment.amount),
    }


def _settlement_utr(payment: Payment) -> str:
    line = payment.settlement_lines.exclude(settlement_utr="").order_by("-settled_at").first()
    return line.settlement_utr if line else ""


def receipt_summary(payment: Payment) -> dict:
    """One line of a receipts list."""
    from .purpose import order_purpose

    order = payment.payment_order
    from .receipt_numbers import ensure as ensure_receipt_number

    return {
        "razorpay_payment_id": payment.razorpay_payment_id,
        "receipt_number": ensure_receipt_number(payment),
        "purpose": order_purpose(order),
        "total_paid": str(payment.amount),
        "paid_at": payment.created_at.isoformat(),
        "test_mode": order.mode == "test",
    }


def receipts_for(queryset) -> list[dict]:
    """Confirmed online payments, newest first."""
    rows = (
        queryset.filter(status__in=RECEIPT_STATUSES, payment_order__status=PaymentOrderStatus.PAID)
        .select_related("payment_order__department", "payment_order__booking__equipment")
        .order_by("-created_at")
    )
    out = []
    for payment in rows[:200]:
        try:
            out.append(receipt_summary(payment))
        except Exception:  # one odd row must never hide the others
            continue
    return out


def receipt_allowed(payment: Payment) -> bool:
    return payment.status in RECEIPT_STATUSES and payment.payment_order.status == PaymentOrderStatus.PAID
