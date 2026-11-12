"""The weekly SRIC email names, for each online payment, the bank reference and Razorpay's settlement UTR."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus
from iic_booking.external_services.sric_dispatch import compose, compose_consolidated
from iic_booking.payments.models import Payment, PaymentOrder, PaymentPurpose, SettlementLine
from iic_booking.payments.razorpay_service import settle_order_success
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def paid_booking(egs_factory):
    owner = UserFactory()
    booking = egs_factory.booking(owner, egs_factory.equipment(), egs_factory.future())
    Booking.objects.filter(pk=booking.pk).update(amount_due=Decimal("944.00"), status=BookingStatus.PENDING_PAYMENT)
    PaymentOrder.objects.create(
        razorpay_order_id="order_UTR", receipt="iic_book_utr", purpose=PaymentPurpose.BOOKING_SHORTFALL, user=owner, booking=booking,
        base_amount=Decimal("944.00"), total_amount=Decimal("944.00"), idempotency_key="k-utr",
    )
    settle_order_success(razorpay_order_id="order_UTR", razorpay_payment_id="pay_UTR", gateway_reference="RRN555")
    booking.refresh_from_db()
    return booking


def _request(booking, payments):
    snapshot = {"booking_reference": "REF-1", "fbr_number": "FBR1", "billing": {"billing_name": "Lab"}, "equipment": {"name": "XRF"},
                "charges": {"total": "944.00"}, "payments": payments}
    return SimpleNamespace(snapshot=snapshot, booking_id=booking.pk, early_requested=False)


def test_unsettled_payment_says_the_utr_is_pending(paid_booking):
    _, text, _ = compose(_request(paid_booking, [{"method": "Online (Razorpay)", "reference": "pay_UTR", "amount": "944.00"}]), mode="test")
    assert "bank ref RRN555" in text and "settlement UTR pending" in text


def test_settled_payment_shows_the_settlement_utr_in_both_emails(paid_booking):
    payment = Payment.objects.get(razorpay_payment_id="pay_UTR")
    SettlementLine.objects.create(settlement_id="setl_9", settlement_utr="UTR777", entity_id="pay_UTR", entity_type="payment",
                                  payment=payment, amount=Decimal("944.00"), settled_at=timezone.now())
    row = [{"method": "Online (Razorpay)", "reference": "pay_UTR", "amount": "944.00"}]
    _, single, _ = compose(_request(paid_booking, row), mode="test")
    _, consolidated, _ = compose_consolidated([_request(paid_booking, row), _request(paid_booking, row)], mode="test")
    for text in (single, consolidated):
        assert "UTR UTR777" in text and "settlement setl_9" in text


def test_bank_transfer_lines_are_unchanged(paid_booking):
    _, text, _ = compose(_request(paid_booking, [{"method": "Bank transfer / cash (UTR)", "reference": "SBIN123", "amount": "10.00"}]), mode="test")
    assert "Bank transfer / cash (UTR): SBIN123" in text and "pending" not in text


def test_external_checkout_payments_reach_the_invoice_snapshot(egs_factory):
    import uuid

    from iic_booking.external_services.invoices import _payments
    from iic_booking.external_services.models import ExternalBookingCheckout

    owner = UserFactory()
    equipment = egs_factory.equipment()
    booking = egs_factory.booking(owner, equipment, egs_factory.future())
    order = PaymentOrder.objects.create(
        razorpay_order_id="order_CK", receipt="iic_book_ck", purpose=PaymentPurpose.BOOKING_SHORTFALL, user=owner, booking=None,
        base_amount=Decimal("500.00"), total_amount=Decimal("500.00"), idempotency_key="k-ck", status="PAID",
    )
    Payment.objects.create(payment_order=order, razorpay_payment_id="pay_CK", amount=Decimal("500.00"))
    assert all(r["reference"] != "pay_CK" for r in _payments(booking))  # not linked to this booking yet
    ExternalBookingCheckout.objects.create(public_id=uuid.uuid4(), user=owner, equipment=equipment, total_amount=Decimal("500.00"),
                                           payment_order=order, booking=booking, state="confirmed")
    rows = _payments(booking)
    assert {"method": "Online (Razorpay)", "reference": "pay_CK", "amount": "500.00"} in rows
