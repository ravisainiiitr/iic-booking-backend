"""PDF generation for Wallet Credit Facility demand / settlement invoices."""

from __future__ import annotations

import io
from decimal import Decimal

from iic_booking.users.models.wallet_credit_facility import WalletCreditInvoice


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
    y = height - 50

    def line(text: str, size: int = 11, gap: int = 16):
        nonlocal y
        c.setFont("Helvetica", size)
        c.drawString(50, y, str(text)[:110])
        y -= gap

    line("IIC Equipment Booking Portal", 14, 20)
    line("Credit Facility — Invoice / Demand for Settlement", 12, 22)
    line(f"Invoice Number: {invoice.invoice_number}")
    line(f"Credit Facility Reference: {facility.public_reference}")
    line(f"Status: {invoice.status}")
    y -= 8
    line("User")
    line(f"  Name: {snap.get('name') or getattr(user, 'name', '')}")
    line(f"  Email: {snap.get('email') or getattr(user, 'email', '')}")
    line(f"  Employee ID: {snap.get('employee_id') or getattr(user, 'emp_id', '') or 'Not available'}")
    line(f"  Department: {snap.get('department') or (facility.department.name if facility.department_id else 'Not available')}")
    y -= 8
    line(f"Issue Date: {invoice.issue_date or 'Not available'}")
    line(f"Due Date: {invoice.due_date or 'Not available'}")
    line(f"Approved Credit: ₹{invoice.approved_credit}")
    line(f"Amount Settled: ₹{invoice.amount_settled}")
    line(f"Outstanding Amount: ₹{invoice.outstanding_amount}")
    y -= 8
    line("Payment Instructions:")
    for chunk in (invoice.payment_instructions or "Settle via Wallet → Pay Outstanding Credit.").split("\n"):
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
