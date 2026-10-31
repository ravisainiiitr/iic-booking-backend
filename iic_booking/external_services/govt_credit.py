"""Credit for government and R&D organisations that can pay only after the invoice, and the choice between
per-booking and consolidated GST invoices.

1. The user generates a consolidated pro forma invoice for the planned work (``create_proforma``).
2. Their department issues an approval letter against it. The user raises a credit request with the letter, the
   credit period and the date of overall settlement (``submit``).
3. The request is emailed to the designated Head IIC addresses with Accept and Reject buttons. Each button opens a
   confirmation page (a mail scanner following the link decides nothing); the decision is a POST with the signed
   token (``decide``).
4. Accepted: the approved amount is credited to the user's sub-wallet and the user is told they can start. Rejected:
   the user is told.
5. While an accepted credit is not settled, no GST invoice request of the user goes to SRIC: requests are held
   (``HELD_CREDIT``) out of the weekly 09:30 batch. The Accounts In Charge records the settlement once SRIC confirms
   the payment is realised (``settle``); the held requests are then released, as one consolidated request if the
   credit asked for a consolidated invoice.

Users who paid in advance choose per-booking or consolidated invoices too (``set_invoice_mode``). Per booking: each
request joins the 09:30 batch as usual. Consolidated: requests wait (``HELD_CONSOLIDATION``) until the user asks for
the consolidated invoice (``release_consolidated``), and then go to SRIC as one request in the next batch.
"""

from __future__ import annotations

import io
import logging
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape

from . import notifications
from .models import (
    ConsolidatedProforma,
    ExternalServicesSettings,
    GovtCreditEvent,
    GovtCreditRequest,
    GovtCreditStatus,
    InvoiceMode,
    InvoiceModePreference,
    InvoiceRequestStatus,
    SricInvoiceRequest,
)

logger = logging.getLogger(__name__)

TOKEN_SALT = "external_services.govt_credit.decision"
TOKEN_MAX_AGE = timedelta(days=14)
LETTER_MAX_BYTES = 10 * 1024 * 1024
LETTER_TYPES = {".pdf": b"%PDF", ".png": b"\x89PNG", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff"}
MAX_PROFORMA_ITEMS = 50


class GovtCreditError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code

    def payload(self) -> dict:
        return {"error": self.message, "code": self.code}


def _money(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise GovtCreditError("Enter a valid amount.", code="invalid_amount")


def _settings() -> ExternalServicesSettings:
    return ExternalServicesSettings.load()


def head_iic_recipients(s: ExternalServicesSettings | None = None) -> list[str]:
    s = s or _settings()
    return [line.strip() for line in (s.head_iic_emails or "").replace(",", "\n").splitlines() if "@" in line]


def is_govt_user(user) -> bool:
    from iic_booking.users.models.user_type import UserType

    return getattr(user, "user_type", None) == UserType.RND


def _event(req, action, *, actor=None, actor_email="", note=""):
    GovtCreditEvent.objects.create(request=req, action=action, actor=actor, actor_email=actor_email, note=note[:4000])


# --- invoice mode --------------------------------------------------------------------------------------------------

def open_credit(user) -> GovtCreditRequest | None:
    """The user's accepted credit not yet settled (bookings on it get no GST invoice until it is)."""
    return GovtCreditRequest.objects.filter(user=user, status=GovtCreditStatus.ACCEPTED).order_by("created_at").first()


def invoice_mode(user) -> str:
    credit = open_credit(user)
    if credit is not None:
        return credit.invoice_mode
    pref = InvoiceModePreference.objects.filter(user=user).order_by("-created_at", "-id").first()
    return pref.mode if pref else InvoiceMode.PER_BOOKING


def set_invoice_mode(user, mode: str, *, actor=None) -> InvoiceModePreference:
    if mode not in InvoiceMode.values:
        raise GovtCreditError("Choose per-booking or consolidated invoices.", code="invalid_mode")
    return InvoiceModePreference.objects.create(user=user, mode=mode, chosen_by=actor or user)


def hold_for(booking) -> tuple[str, str]:
    """``(status, consolidation_group)`` for a new invoice request of ``booking``: held while the user's credit is
    open, waiting for a consolidated invoice when the user chose one, otherwise queued for the next batch."""
    credit = open_credit(booking.user)
    if credit is not None:
        group = f"credit:{credit.public_id}" if credit.invoice_mode == InvoiceMode.CONSOLIDATED else ""
        return InvoiceRequestStatus.HELD_CREDIT, group
    if invoice_mode(booking.user) == InvoiceMode.CONSOLIDATED:
        return InvoiceRequestStatus.HELD_CONSOLIDATION, ""
    return InvoiceRequestStatus.QUEUED, ""


def release_consolidated(user, *, actor=None) -> int:
    """Send the user's held requests to SRIC as one consolidated invoice request in the next 09:30 batch."""
    if open_credit(user) is not None:
        raise GovtCreditError("Invoices on an open credit are released when the credit is settled.", status=409,
                              code="credit_open")
    group = f"user:{user.pk}:{uuid.uuid4().hex[:12]}"
    with transaction.atomic():
        ids = list(SricInvoiceRequest.objects.select_for_update()
                   .filter(booking__user=user, status=InvoiceRequestStatus.HELD_CONSOLIDATION)
                   .values_list("pk", flat=True))
        if not ids:
            raise GovtCreditError("No completed bookings are waiting for a consolidated invoice.", status=404,
                                  code="nothing_held")
        SricInvoiceRequest.objects.filter(pk__in=ids).update(
            status=InvoiceRequestStatus.QUEUED, consolidation_group=group, created_at=timezone.now()
        )
    notifications.notify(
        f"invoice:consolidated:{group}", [user],
        title="Consolidated GST invoice requested",
        message=f"Your consolidated GST invoice request for {len(ids)} booking(s) goes to SRIC in the next "
                "working-day batch (09:30 IST).",
        link="/my-bookings",
    )
    return len(ids)


# --- consolidated pro forma invoice ----------------------------------------------------------------------------------

def create_proforma(user, items) -> ConsolidatedProforma:
    rows = []
    for item in (items or [])[:MAX_PROFORMA_ITEMS]:
        description = str((item or {}).get("description") or "").strip()[:255]
        amount = _money((item or {}).get("amount"))
        if not description or amount <= 0:
            raise GovtCreditError("Each line needs a description and a positive amount.", code="invalid_items")
        rows.append({"description": description, "amount": f"{amount:.2f}"})
    if not rows:
        raise GovtCreditError("Add at least one line of planned work.", code="invalid_items")
    total = sum((Decimal(r["amount"]) for r in rows), Decimal("0.00"))
    reference = f"IIC-CPI-{timezone.localdate():%Y%m%d}-{user.pk}-{secrets.token_hex(2).upper()}"
    return ConsolidatedProforma.objects.create(reference=reference, user=user, items=rows, total=total)


def proforma_pdf(proforma: ConsolidatedProforma) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from iic_booking.equipment.document_exports import _pdf_letterhead_story_lines
    from iic_booking.users.models import ExternalBillingProfile

    title = "Consolidated Pro Forma Invoice — Not a GST Tax Invoice"
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, leading=12)
    warn = ParagraphStyle("warn", parent=styles["Normal"], fontSize=10, leading=13, textColor=colors.HexColor("#8a1c1c"))
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=1.6 * cm, rightMargin=1.6 * cm, topMargin=1.4 * cm,
                            bottomMargin=1.4 * cm, title=title)
    story = list(_pdf_letterhead_story_lines(department_name="Institute Instrumentation Centre", document_title=title))
    story.append(Paragraph(
        "This consolidated pro forma invoice covers planned analysis work, for the organisation's approval. It is "
        "<b>not a GST tax invoice</b>; the GST invoice is issued by the SRIC office, IIT Roorkee, only after the "
        "payment is realised.", warn))
    story.append(Spacer(1, 8))
    user = proforma.user
    billing = ExternalBillingProfile.objects.filter(user=user).first()
    address = ", ".join(x for x in [
        getattr(billing, "billing_address_line1", ""), getattr(billing, "billing_address_line2", ""),
        getattr(billing, "billing_city", ""), getattr(billing, "billing_state", ""),
        getattr(billing, "billing_pincode", ""), getattr(billing, "billing_country", ""),
    ] if x)
    meta = [
        ["Reference", proforma.reference],
        ["Billed to", getattr(billing, "billing_name", "") or getattr(user, "name", "") or user.email],
        ["GSTIN", getattr(billing, "gstin", "") or "Not provided"],
        ["Address", address or "Not provided"],
        ["Generated", timezone.localtime(proforma.created_at).strftime("%d %b %Y %I:%M %p IST")],
    ]
    table = Table([[Paragraph(f"<b>{k}</b>", small), Paragraph(escape(str(v)), small)] for k, v in meta],
                  colWidths=[3.6 * cm, 13.6 * cm])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [table, Spacer(1, 10)]
    rows = [["Planned work", "Amount (Rs.)"]]
    rows += [[Paragraph(escape(item["description"]), small), item["amount"]] for item in proforma.items]
    rows.append([Paragraph("<b>Total</b>", small), f"{proforma.total:.2f}"])
    charges = Table(rows, colWidths=[13.2 * cm, 4.0 * cm])
    charges.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
    ]))
    story += [charges, Spacer(1, 8),
              Paragraph("Charges of each booking are set by the portal at the time of booking and drawn from the "
                        "credit approved against this pro forma invoice.", small)]
    doc.build(story)
    return buffer.getvalue()


# --- credit request ----------------------------------------------------------------------------------------------------

def _check_letter(upload) -> None:
    import os

    if upload is None:
        raise GovtCreditError("Attach your department's approval letter.", code="letter_required")
    ext = os.path.splitext(getattr(upload, "name", "") or "")[1].lower()
    if ext not in LETTER_TYPES or (getattr(upload, "size", 0) or 0) > LETTER_MAX_BYTES:
        raise GovtCreditError("The approval letter must be a PDF, PNG or JPG of at most 10 MB.", code="letter_invalid")
    head = upload.read(8)
    upload.seek(0)
    if not head.startswith(LETTER_TYPES[ext]):
        raise GovtCreditError("The approval letter does not look like the file type it claims.", code="letter_invalid")


def _subwallet(user, department_id):
    from iic_booking.users.models import Department, SubWallet, Wallet

    department = Department.objects.filter(pk=department_id).first() if department_id else None
    if department is None:
        raise GovtCreditError("Choose the department whose equipment you will use.", code="department_required")
    wallet, _ = Wallet.objects.get_or_create(user=user)
    sub, _ = SubWallet.objects.get_or_create(wallet=wallet, department=department)
    return sub


def submit(user, *, proforma_reference, department_id, organisation, amount, credit_until, settlement_date,
           approval_letter, invoice_mode_choice=InvoiceMode.CONSOLIDATED, purpose="") -> GovtCreditRequest:
    s = _settings()
    if not s.govt_credit_enabled:
        raise GovtCreditError("Credit requests are not available yet.", status=403, code="disabled")
    if not is_govt_user(user):
        raise GovtCreditError("Credit is available to government and R&D organisations.", status=403,
                              code="not_eligible")
    if not head_iic_recipients(s):
        raise GovtCreditError("Credit requests cannot be sent yet (no Head IIC address is configured).", status=503,
                              code="no_approver")
    proforma = ConsolidatedProforma.objects.filter(reference=(proforma_reference or "").strip(), user=user).first()
    if proforma is None:
        raise GovtCreditError("Generate the consolidated pro forma invoice first and quote its reference.",
                              code="proforma_required")
    amount = _money(amount)
    if amount <= 0 or amount > proforma.total:
        raise GovtCreditError(f"The credit must be more than zero and at most the pro forma total "
                              f"(Rs. {proforma.total:.2f}).", code="invalid_amount")
    today = timezone.localdate()
    if credit_until is None or settlement_date is None or credit_until < today or settlement_date < today:
        raise GovtCreditError("Give a credit period end and a settlement date that are not in the past.",
                              code="invalid_dates")
    if invoice_mode_choice not in InvoiceMode.values:
        raise GovtCreditError("Choose per-booking or consolidated invoices.", code="invalid_mode")
    if not (organisation or "").strip():
        raise GovtCreditError("Name the organisation.", code="organisation_required")
    _check_letter(approval_letter)
    with transaction.atomic():
        if GovtCreditRequest.objects.select_for_update().filter(user=user, status=GovtCreditStatus.PENDING).exists():
            raise GovtCreditError("A credit request is already waiting for the Head IIC.", status=409,
                                  code="already_pending")
        req = GovtCreditRequest.objects.create(
            public_id=uuid.uuid4(), user=user, subwallet=_subwallet(user, department_id),
            organisation=organisation.strip()[:255], amount=amount, proforma_reference=proforma.reference,
            purpose=(purpose or "")[:4000], approval_letter=approval_letter, credit_until=credit_until,
            settlement_date=settlement_date, invoice_mode=invoice_mode_choice,
        )
        _event(req, "raised", actor=user)
    _email_approvers(req, s)
    return req


def decision_token(req: GovtCreditRequest, action: str, email: str) -> str:
    return signing.dumps({"r": str(req.public_id), "a": action, "e": email}, salt=TOKEN_SALT, compress=True)


def read_token(token: str) -> tuple[GovtCreditRequest, str, str]:
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except signing.SignatureExpired:
        raise GovtCreditError("This link has expired.", status=410, code="link_expired")
    except signing.BadSignature:
        raise GovtCreditError("This link is not valid.", status=404, code="link_invalid")
    req = GovtCreditRequest.objects.filter(public_id=data.get("r")).select_related("user").first()
    if req is None or data.get("a") not in ("accept", "reject") or data.get("e") not in head_iic_recipients():
        raise GovtCreditError("This link is not valid.", status=404, code="link_invalid")
    return req, data["a"], data["e"]


def _email_approvers(req: GovtCreditRequest, s: ExternalServicesSettings) -> None:
    from iic_booking.communication.utils import get_backend_absolute_url

    for email in head_iic_recipients(s):
        links = {action: get_backend_absolute_url(f"/api/v1/external-booking/govt-credit/decide/"
                                                   f"{decision_token(req, action, email)}/")
                 for action in ("accept", "reject")}
        subject = f"Credit request: {req.organisation} — Rs. {req.amount:.2f} ({req.proforma_reference})"
        lines = [
            "Dear Head IIC,",
            "",
            f"{req.organisation} has requested credit on the IIC Booking Portal, to be settled after the invoice.",
            "",
            f"Requested by: {getattr(req.user, 'name', '') or req.user.email} ({req.user.email})",
            f"Amount: Rs. {req.amount:.2f}",
            f"Consolidated pro forma invoice: {req.proforma_reference}",
            f"Credit period ends: {req.credit_until:%d %b %Y}",
            f"Overall settlement by: {req.settlement_date:%d %b %Y}",
            f"Invoice: {req.get_invoice_mode_display()}",
            f"Planned work: {req.purpose or '-'}",
            "",
            "The department's approval letter is attached to the request on the portal.",
            "",
            f"Accept: {links['accept']}",
            f"Reject: {links['reject']}",
            "",
            "Each link opens a page where you confirm the decision. The links are valid for 14 days.",
        ]
        html = ("<p>" + "<br/>".join(escape(line) for line in lines[:-4]) + "</p>"
                f'<p><a href="{escape(links["accept"])}" style="background:#15803d;color:#fff;padding:8px 18px;'
                f'text-decoration:none;border-radius:4px">Accept</a> &nbsp; '
                f'<a href="{escape(links["reject"])}" style="background:#b91c1c;color:#fff;padding:8px 18px;'
                f'text-decoration:none;border-radius:4px">Reject</a></p>'
                "<p>Each button opens a page where you confirm the decision. The links are valid for 14 days.</p>")
        msg = EmailMultiAlternatives(subject=subject, body="\n".join(lines), from_email=settings.DEFAULT_FROM_EMAIL,
                                     to=[email])
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=False)
    _event(req, "emailed", note=", ".join(head_iic_recipients(s)))


def decide(token: str, *, note: str = "") -> GovtCreditRequest:
    """Apply the Head IIC's decision from the confirmation page (POST)."""
    req, action, email = read_token(token)
    with transaction.atomic():
        req = GovtCreditRequest.objects.select_for_update().select_related("user", "subwallet").get(pk=req.pk)
        if req.status != GovtCreditStatus.PENDING:
            raise GovtCreditError(f"This request has already been decided ({req.get_status_display()}).",
                                  status=409, code="already_decided")
        req.decided_by_email, req.decided_at, req.decision_note = email, timezone.now(), (note or "")[:4000]
        if action == "accept":
            from iic_booking.users.models import SubWallet

            sub = SubWallet.objects.select_for_update().get(pk=req.subwallet_id)
            req.wallet_transaction = sub.credit(
                req.amount, f"Government credit {req.proforma_reference} approved by the Head IIC",
                related_user=req.user,
            )
            req.status = GovtCreditStatus.ACCEPTED
        else:
            req.status = GovtCreditStatus.REJECTED
        req.save()
        _event(req, "accepted" if action == "accept" else "rejected", actor_email=email, note=note)
    if req.status == GovtCreditStatus.ACCEPTED:
        title = f"Credit approved: Rs. {req.amount:.2f} added to your wallet"
        message = ("The Head IIC has approved your credit request. The amount is in your wallet and you can start "
                   f"booking now. Please settle it by {req.settlement_date:%d %b %Y}; GST invoices are issued after "
                   "the payment is realised.")
    else:
        title = "Credit request not approved"
        message = "The Head IIC has not approved your credit request." + (f" Note: {note}" if note else "")
    notifications.notify(f"govt_credit:{req.public_id}:{req.status}", [req.user], title=title, message=message,
                         link="/wallet")
    return req


def settle(actor, public_id, *, reference: str) -> GovtCreditRequest:
    """Accounts In Charge: SRIC has confirmed the payment is realised. Releases the held invoice requests."""
    from iic_booking.users.models.user_type import UserType

    if getattr(actor, "user_type", None) not in (UserType.FINANCE, UserType.ADMIN) and not getattr(actor, "is_superuser", False):
        raise GovtCreditError("Only the Accounts In Charge can record a settlement.", status=403, code="forbidden")
    if not (reference or "").strip():
        raise GovtCreditError("Give the SRIC receipt or UTR reference.", code="reference_required")
    with transaction.atomic():
        req = GovtCreditRequest.objects.select_for_update().filter(public_id=public_id).first()
        if req is None:
            raise GovtCreditError("Credit request not found.", status=404, code="not_found")
        if req.status != GovtCreditStatus.ACCEPTED:
            raise GovtCreditError("Only an accepted credit can be settled.", status=409, code="not_open")
        req.status, req.settled_at, req.settled_by = GovtCreditStatus.SETTLED, timezone.now(), actor
        req.settlement_reference = reference.strip()[:128]
        req.save()
        _event(req, "settled", actor=actor, note=req.settlement_reference)
        released = 0
        if open_credit(req.user) is None:  # another accepted credit keeps the invoices held
            held = SricInvoiceRequest.objects.select_for_update().filter(
                booking__user=req.user, status=InvoiceRequestStatus.HELD_CREDIT)
            group = f"credit:{req.public_id}" if req.invoice_mode == InvoiceMode.CONSOLIDATED else ""
            released = held.update(status=InvoiceRequestStatus.QUEUED, consolidation_group=group,
                                   created_at=timezone.now())
    notifications.notify(
        f"govt_credit:{req.public_id}:settled", [req.user],
        title="Credit settled",
        message="Your payment has been realised. "
                + (f"{released} GST invoice request(s) go to SRIC in the next working-day batch." if released else ""),
        link="/my-bookings",
    )
    return req
