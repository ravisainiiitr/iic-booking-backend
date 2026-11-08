"""Printed receipt numbers: IITR/<department>/<year>/<serial>, e.g. IITR/IIC/2026/00001.

* IITR: the institute.
* department: the code of the department the money is for. For a booking, that is the instrument's department.
  For a wallet recharge, it is the recharged department.
* year: the calendar year of the payment (IST).
* serial: five digits, one series per year for the whole portal, starting again from 00001 each year.

Test-mode payments have their own series, shown as TEST-00001, so they never use up or leave gaps in real numbers.
A number is given once, when the payment is confirmed, and stored on the Payment. A payment confirmed before
numbering existed gets its number the first time its receipt is opened.
"""

from __future__ import annotations

import re

from django.db import IntegrityError
from django.db import transaction
from django.utils import timezone

INSTITUTE = "IITR"
FALLBACK_DEPARTMENT = "IITR"


def department_code(order) -> str:
    """The department code for the order, made safe for a receipt number."""
    department = None
    if order.purpose == "WALLET_RECHARGE":
        department = order.department
    else:
        booking = order.booking
        if booking is None:
            checkout = getattr(order, "external_checkout", None)
            booking = getattr(checkout, "booking", None)
            equipment = getattr(booking, "equipment", None) or getattr(checkout, "equipment", None)
        else:
            equipment = booking.equipment
        department = getattr(equipment, "internal_department", None) or order.department
    code = re.sub(r"[^A-Z0-9]", "", str(getattr(department, "code", "") or "").upper())[:12]
    return code or FALLBACK_DEPARTMENT


def _year(payment) -> int:
    return timezone.localtime(payment.created_at or timezone.now()).year


def ensure(payment) -> str:
    """The payment's receipt number, giving it the next serial of its year if it has none yet."""
    from .models import Payment
    from .models import ReceiptSerialCounter

    if payment.receipt_number:
        return payment.receipt_number
    order = payment.payment_order
    test_mode = order.mode == "test"
    year = _year(payment)
    department = department_code(order)
    for _attempt in range(3):
        try:
            with transaction.atomic():
                current = Payment.objects.select_for_update().only("receipt_number").get(pk=payment.pk)
                if current.receipt_number:
                    payment.receipt_number = current.receipt_number
                    return current.receipt_number
                ReceiptSerialCounter.objects.get_or_create(year=year, test_mode=test_mode)
                counter = ReceiptSerialCounter.objects.select_for_update().get(year=year, test_mode=test_mode)
                counter.last += 1
                counter.save(update_fields=["last"])
                serial = f"TEST-{counter.last:05d}" if test_mode else f"{counter.last:05d}"
                number = f"{INSTITUTE}/{department}/{year}/{serial}"
                Payment.objects.filter(pk=payment.pk).update(receipt_number=number)
                payment.receipt_number = number
                return number
        except IntegrityError:
            # Two first calls for the same year at once: the loser retries with the row now present.
            continue
    raise RuntimeError("Could not give the payment a receipt number.")
