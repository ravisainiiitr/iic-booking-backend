"""PDF generation for Wallet Credit Facility demand / settlement invoices."""

from __future__ import annotations

import io
import os
from decimal import Decimal

from django.conf import settings

from iic_booking.users.models.wallet_credit_facility import WalletCreditInvoice


def _masthead_path() -> str | None:
    base_dir = getattr(settings, "BASE_DIR", None)
    if not base_dir:
        return None
    path = os.path.join(str(base_dir), "fonts", "iitr-pdf-masthead.png")
    return path if os.path.isfile(path) else None


def build_credit_invoice_pdf(invoice: WalletCreditInvoice) -> bytes:
    """Return PDF bytes for a credit settlement demand (not a booking invoice)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    facility = invoice.facility
    user = facility.user
    snap = facility.profile_snapshot or {}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 36

    masthead = _masthead_path()
    if masthead:
        from reportlab.lib.utils import ImageReader
        ir = ImageReader(masthead)
        iw, ih = ir.getSize()
        img_w = 280
        img_h = img_w * (float(ih) / float(iw))
        c.drawImage(
            masthead,
            (width - img_w) / 2,
            y - img_h,
            width=img_w,
            height=img_h,
            mask="auto",
            preserveAspectRatio=True,
            anchor="c",
        )
        y -= img_h + 14
    else:
        c.setFillColorRGB(0.08, 0.25, 0.47)
        c.setFont("Helvetica-Bold", 12)
        c.drawCentredString(width / 2, y, "Indian Institute of Technology Roorkee")
        y -= 18

    dept = "Institute Instrumentation Centre (IIC)"
    try:
        if facility.department_id and facility.department:
            dept = facility.department.name or dept
    except Exception:
        pass
    c.setFillColorRGB(0.08, 0.25, 0.47)
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(width / 2, y, dept[:90])
    y -= 18
    c.setFillColorRGB(0.12, 0.16, 0.23)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(width / 2, y, "Credit Facility — Invoice / Demand for Settlement")
    y -= 28

    def line(text: str, size: int = 11, gap: int = 16):
        nonlocal y
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", size)
        c.drawString(50, y, str(text)[:110])
        y -= gap

    line(f"Invoice Number: {invoice.invoice_number}")
    line(f"Credit Facility Reference: {facility.public_reference}")
    line(f"Status: {invoice.status}")
    y -= 8
    line("User")
    line(f"  Name: {snap.get('name') or getattr(user, 'name', '')}")
    line(f"  Email: {snap.get('email') or getattr(user, 'email', '')}")
    line(f"  Employee ID: {snap.get('employee_id') or getattr(user, 'emp_id', '') or 'Not available'}")
    line(
        f"  Department: {snap.get('department') or (facility.department.name if facility.department_id else 'Not available')}"
    )
    y -= 8
    line(f"Issue Date: {invoice.issue_date or 'Not available'}")
    line(f"Due Date: {invoice.due_date or 'Not available'}")
    line(f"Approved Credit: Rs.{invoice.approved_credit}")
    line(f"Amount Settled: Rs.{invoice.amount_settled}")
    line(f"Outstanding Amount: Rs.{invoice.outstanding_amount}")
    y -= 8
    line("Payment Instructions:")
    for chunk in (invoice.payment_instructions or "Settle via Wallet -> Pay Outstanding Credit.").split("\n"):
        line(f"  {chunk}")
    y -= 8
    line("Terms:")
    for chunk in (invoice.terms or "").split("\n") or ["As per portal credit policy."]:
        line(f"  {chunk}")
    y -= 20
    line("Authorized Signatory: Accounts / Main Administrator", 10)
    line("This document is a demand for settlement of an approved credit facility,", 9)
    line("not a tax invoice for goods or services.", 9)
    c.showPage()
    c.save()
    return buf.getvalue()


def money_str(value) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.01")))