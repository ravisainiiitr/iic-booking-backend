"""Receipts can be checked on the public page with the code printed on them."""

import pytest
from rest_framework.test import APIClient

from iic_booking.payments.models import Payment
from iic_booking.payments.models import PaymentStatus
from iic_booking.payments.receipt_verification import check
from iic_booking.payments.receipt_verification import verification_code
from iic_booking.payments.receipts import receipt_for
from iic_booking.payments.test_receipt_numbers import _paid
from iic_booking.payments.test_receipt_numbers import departments  # noqa: F401

pytestmark = pytest.mark.django_db


def test_a_genuine_receipt_checks_out(departments):  # noqa: F811
    iic, _ = departments
    payment = _paid(1, iic)
    r = receipt_for(payment)
    code = r["verification_code"]
    assert len(code) == 11 and code[5] == "-"
    assert f"/verify-receipt?n=" in r["verify_url"] and code in r["verify_url"]
    res = APIClient().get("/api/receipts/verify/", {"number": payment.receipt_number, "code": code})
    assert res.status_code == 200 and res.data["valid"] is True
    assert res.data["total"] == "100.00" and res.data["status"] == "paid"
    assert res.data["receipt_number"] == payment.receipt_number
    # Lower case, no dash and 0/1 typed for O/I still match.
    sloppy = code.replace("-", "").lower().replace("o", "0").replace("i", "1")
    assert check(payment.receipt_number.lower(), sloppy)["valid"] is True


def test_wrong_code_unknown_number_and_altered_amount_fail(departments):  # noqa: F811
    iic, _ = departments
    payment = _paid(1, iic)
    code = verification_code(payment)
    assert check(payment.receipt_number, "AAAAA-AAAAA") == {"valid": False}
    assert check("IITR/IIC/2026/99999", code) == {"valid": False}
    assert check("", "") == {"valid": False}
    # The code is tied to the amount: a receipt edited to show more money has no matching code.
    payment.amount = payment.amount + 1
    assert verification_code(payment) != code


def test_refunds_are_shown(departments):  # noqa: F811
    iic, _ = departments
    payment = _paid(1, iic)
    code = verification_code(payment)
    Payment.objects.filter(pk=payment.pk).update(status=PaymentStatus.PARTIALLY_REFUNDED)
    result = check(payment.receipt_number, code)
    assert result["valid"] and result["status"] == "partly_refunded"


def test_booking_status_and_bank_reference_are_shown(departments):  # noqa: F811
    from types import SimpleNamespace

    from iic_booking.payments.receipt_verification import _booking_status

    iic, _ = departments
    payment = _paid(1, iic)
    Payment.objects.filter(pk=payment.pk).update(gateway_reference="UTR123456789")
    result = check(payment.receipt_number, verification_code(payment))
    assert result["bank_reference"] == "UTR123456789"
    assert result["booking_status"] == "" and result["booking_undone"] is False  # a wallet recharge has no booking

    def order(code, label):
        booking = SimpleNamespace(status=code, get_status_display=lambda: label)
        return SimpleNamespace(booking=booking)

    assert _booking_status(order("BOOKED", "Booked")) == {
        "booking_status": "BOOKED", "booking_status_label": "Booked", "booking_undone": False}
    assert _booking_status(order("CANCELLED", "Cancelled"))["booking_undone"] is True
    assert _booking_status(order("REFUNDED", "Refunded"))["booking_undone"] is True
    assert _booking_status(order("COMPLETED", "Completed"))["booking_undone"] is False
